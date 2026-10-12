#!/usr/bin/env python3
"""
ToteTracker - Storage Box Inventory Application
A simple, mobile-friendly app to track what's in your storage totes.
"""

import os
import sqlite3
import threading
import json
import requests
import base64
import re
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, Response
from werkzeug.utils import secure_filename

import labels
from urllib.parse import quote

# ── Configuration ────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
DATA_DIR       = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
BOXES_DIR      = DATA_DIR / "boxes"
DB_PATH        = DATA_DIR / "totetracker.db"
ALLOWED_EXT    = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "mp4", "mov"}
MAX_ATTACH     = 10
MAX_FILE_MB    = 50
OLLAMA_URL        = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL      = os.environ.get("OLLAMA_MODEL", "llama3.2-vision")
OLLAMA_API_KEY    = os.environ.get("OLLAMA_API_KEY", "").strip()    # Bearer token
OLLAMA_TIMEOUT    = int(os.environ.get("OLLAMA_TIMEOUT", 300))          # seconds to wait for vision response
OLLAMA_BASIC_AUTH = os.environ.get("OLLAMA_BASIC_AUTH", "").strip() # "user:password"
PORT           = int(os.environ.get("PORT", 5000))

# ── App Setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.context_processor
def inject_base():
    """URL prefix when served through Home Assistant ingress (X-Ingress-Path); empty otherwise."""
    p = request.headers.get("X-Ingress-Path", "")
    return {"base": p.rstrip("/") if p.startswith("/api/hassio_ingress/") else ""}

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024

DATA_DIR.mkdir(parents=True, exist_ok=True)
BOXES_DIR.mkdir(parents=True, exist_ok=True)


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS boxes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL UNIQUE,
                contents    TEXT DEFAULT '',
                ai_contents TEXT DEFAULT '',
                location    TEXT DEFAULT '',
                label_size  TEXT DEFAULT '4x6',
                label_orient TEXT DEFAULT 'landscape',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                box_id     INTEGER NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
                filename   TEXT NOT NULL,
                orig_name  TEXT NOT NULL,
                mimetype   TEXT DEFAULT '',
                ai_desc    TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS locations (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS boxes_fts
                USING fts5(label, contents, ai_contents, location, content='boxes', content_rowid='id');

            CREATE TRIGGER IF NOT EXISTS boxes_ai AFTER INSERT ON boxes BEGIN
                INSERT INTO boxes_fts(rowid, label, contents, ai_contents, location)
                VALUES (new.id, new.label, new.contents, new.ai_contents, new.location);
            END;

            CREATE TRIGGER IF NOT EXISTS boxes_au AFTER UPDATE ON boxes BEGIN
                INSERT INTO boxes_fts(boxes_fts, rowid, label, contents, ai_contents, location)
                VALUES ('delete', old.id, old.label, old.contents, old.ai_contents, old.location);
                INSERT INTO boxes_fts(rowid, label, contents, ai_contents, location)
                VALUES (new.id, new.label, new.contents, new.ai_contents, new.location);
            END;

            CREATE TRIGGER IF NOT EXISTS boxes_ad AFTER DELETE ON boxes BEGIN
                INSERT INTO boxes_fts(boxes_fts, rowid, label, contents, ai_contents, location)
                VALUES ('delete', old.id, old.label, old.contents, old.ai_contents, old.location);
            END;
        """)
    # Migration: add label_size to databases created before label sizes existed.
    with get_db() as db:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(boxes)").fetchall()]
        if "label_size" not in cols:
            db.execute("ALTER TABLE boxes ADD COLUMN label_size TEXT DEFAULT '4x6'")
            db.commit()
            print("[DB] Migrated: added boxes.label_size")
        if "label_orient" not in cols:
            db.execute("ALTER TABLE boxes ADD COLUMN label_orient TEXT DEFAULT 'landscape'")
            db.commit()
            print("[DB] Migrated: added boxes.label_orient")

    # Rebuild FTS index on startup — ensures AI content added after initial insert is searchable.
    # This is fast (rebuilds in-place) and safe to run every time.
    with get_db() as db:
        db.execute("INSERT INTO boxes_fts(boxes_fts) VALUES('rebuild')")

    # One-time migration: seed the locations table from any location values already
    # typed into existing boxes, so upgrading doesn't lose/hide anything.
    with get_db() as db:
        existing = db.execute(
            "SELECT DISTINCT location FROM boxes WHERE TRIM(location) != ''"
        ).fetchall()
        for row in existing:
            loc = row["location"].strip()
            if not loc:
                continue
            try:
                db.execute("INSERT INTO locations (name) VALUES (?)", (loc,))
            except sqlite3.IntegrityError:
                pass
        db.commit()

    print(f"[DB] Initialized and FTS index rebuilt at {DB_PATH}")


def get_location_order() -> dict:
    """Map lowercased location name -> its position in the user-defined location list."""
    db = get_db()
    rows = db.execute("SELECT name FROM locations ORDER BY id").fetchall()
    return {r["name"].strip().lower(): i for i, r in enumerate(rows)}


def sort_boxes_by_location(rows):
    """Sort box rows by the user's saved location order, then location name, then label.
    Locations not in the saved list (or blank) sort to the end, alphabetically."""
    loc_order = get_location_order()

    def sort_key(r):
        loc = (r["location"] or "").strip()
        idx = loc_order.get(loc.lower(), len(loc_order) + (0 if loc else 1))
        return (idx if loc else len(loc_order) + 1, loc.lower(), (r["label"] or "").lower())

    return sorted(rows, key=sort_key)


# ── Helpers ───────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def box_folder(label):
    safe = re.sub(r'[^\w\-]', '_', label)
    folder = BOXES_DIR / safe
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def is_image(filename):
    return filename.rsplit(".", 1)[-1].lower() in {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"}


def ollama_headers() -> dict:
    """Build auth headers from environment variables."""
    headers = {}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
    elif OLLAMA_BASIC_AUTH and ":" in OLLAMA_BASIC_AUTH:
        encoded = base64.b64encode(OLLAMA_BASIC_AUTH.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    return headers


def analyze_image_with_ollama(image_path: Path) -> str:
    """Send an image to Ollama llama3.2-vision and return description."""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        payload = {
            "model": OLLAMA_MODEL,
            "prompt": (
                "You are helping catalog storage box contents. "
                "Look at this photo and describe what items you can see. "
                "Be concise but thorough — list the main items visible. "
                "Format as a plain comma-separated list of items."
            ),
            "images": [b64],
            "stream": False
        }
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            headers=ollama_headers(),
            timeout=(10, OLLAMA_TIMEOUT)  # connect timeout=10s, read timeout=OLLAMA_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        return "[Ollama not reachable — check OLLAMA_URL]"
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in (401, 403):
            return "[Ollama auth failed — check OLLAMA_API_KEY or OLLAMA_BASIC_AUTH]"
        return f"[AI analysis failed: {e}]"
    except Exception as e:
        return f"[AI analysis failed: {e}]"


def row_to_dict(row):
    return dict(row)


# ── Routes: Pages ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/<ref>")
def index_box(ref):
    """Deep link target for QR codes (/<bin number>) — same SPA; the page JS resolves the bin
    number via /api/boxes/by-ref/<ref> and opens that tote's detail view."""
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


def _find_ref_conflict(ref, exclude_id=None):
    """Return the label of another tote that already uses this bin number (case-insensitive)."""
    for r in get_db().execute("SELECT id, label FROM boxes").fetchall():
        if r["id"] != exclude_id and labels.bin_ref(r["label"]).lower() == ref.lower():
            return r["label"]
    return None


def _ref_conflict_msg(ref, other):
    return (f"Bin number '{ref}' is already used by '{other}'. Each bin number must be unique — "
            f"it's what the QR code links to.")


@app.route("/api/boxes/by-ref/<ref>")
def box_by_ref(ref):
    """Resolve a bin number (the part of the name before ' - ') to a tote id."""
    for r in get_db().execute("SELECT id, label FROM boxes ORDER BY id").fetchall():
        if labels.bin_ref(r["label"]).lower() == ref.strip().lower():
            return jsonify({"id": r["id"], "label": r["label"]})
    return jsonify({"error": "Not found"}), 404


# ── Routes: API ───────────────────────────────────────────────────────────────
@app.route("/api/boxes", methods=["GET"])
def list_boxes():
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        rows = db.execute("""
            SELECT b.* FROM boxes b
            JOIN boxes_fts f ON b.id = f.rowid
            WHERE boxes_fts MATCH ?
            ORDER BY rank
        """, (q,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM boxes").fetchall()
        rows = sort_boxes_by_location(rows)

    result = []
    for row in rows:
        box = row_to_dict(row)
        box["attachments"] = [
            row_to_dict(a) for a in db.execute(
                "SELECT * FROM attachments WHERE box_id=? ORDER BY created_at", (box["id"],)
            ).fetchall()
        ]
        result.append(box)
    return jsonify(result)


@app.route("/api/boxes/<int:box_id>", methods=["GET"])
def get_box(box_id):
    db = get_db()
    row = db.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    box = row_to_dict(row)
    box["attachments"] = [
        row_to_dict(a) for a in db.execute(
            "SELECT * FROM attachments WHERE box_id=? ORDER BY created_at", (box_id,)
        ).fetchall()
    ]
    return jsonify(box)


@app.route("/api/locations", methods=["GET"])
def list_locations():
    db = get_db()
    rows = db.execute("SELECT * FROM locations ORDER BY id").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/locations", methods=["POST"])
def create_location():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Location name is required"}), 400
    db = get_db()
    try:
        cur = db.execute("INSERT INTO locations (name) VALUES (?)", (name,))
        db.commit()
        return jsonify({"id": cur.lastrowid, "name": name}), 201
    except sqlite3.IntegrityError:
        existing = db.execute("SELECT * FROM locations WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return jsonify({"id": existing["id"], "name": existing["name"]}), 200


@app.route("/api/locations/<int:loc_id>", methods=["DELETE"])
def delete_location(loc_id):
    db = get_db()
    row = db.execute("SELECT * FROM locations WHERE id=?", (loc_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    in_use = db.execute(
        "SELECT COUNT(*) FROM boxes WHERE location = ? COLLATE NOCASE", (row["name"],)
    ).fetchone()[0]
    if in_use:
        return jsonify({"error": f"{in_use} tote(s) still use '{row['name']}' — reassign them first"}), 400
    db.execute("DELETE FROM locations WHERE id=?", (loc_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/boxes", methods=["POST"])
def create_box():
    data = request.get_json() or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "Label is required"}), 400
    other = _find_ref_conflict(labels.bin_ref(label))
    if other:
        return jsonify({"error": _ref_conflict_msg(labels.bin_ref(label), other)}), 409
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO boxes (label, contents, location, label_size, label_orient) VALUES (?,?,?,?,?)",
            (label, data.get("contents", ""), data.get("location", ""),
             labels.normalize_size(data.get("label_size")),
             labels.normalize_orient(data.get("label_orient")))
        )
        loc = (data.get("location") or "").strip()
        if loc:
            try:
                db.execute("INSERT INTO locations (name) VALUES (?)", (loc,))
            except sqlite3.IntegrityError:
                pass
        db.commit()
        box_folder(label)  # create the folder
        return jsonify({"id": cur.lastrowid, "label": label}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": f"A box named '{label}' already exists"}), 409


@app.route("/api/boxes/<int:box_id>", methods=["PUT"])
def update_box(box_id):
    data = request.get_json() or {}
    db = get_db()
    row = db.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    label      = (data.get("label") or row["label"]).strip()
    contents   = data.get("contents",    row["contents"])
    ai_contents= data.get("ai_contents", row["ai_contents"])
    location   = data.get("location",    row["location"])
    label_size = data.get("label_size")
    label_size = label_size if label_size in labels.LABEL_SIZES else (row["label_size"] or labels.DEFAULT_SIZE)
    label_orient = data.get("label_orient")
    label_orient = label_orient if label_orient in labels.ORIENTS else (row["label_orient"] or labels.DEFAULT_ORIENT)

    # The bin number is what QR codes link to: keep it unique (only checked when it changes)
    new_ref, old_ref = labels.bin_ref(label), labels.bin_ref(row["label"])
    if new_ref.lower() != old_ref.lower():
        other = _find_ref_conflict(new_ref, exclude_id=box_id)
        if other:
            return jsonify({"error": _ref_conflict_msg(new_ref, other)}), 409

    # Rename folder if label changed
    if label != row["label"]:
        old_folder = box_folder(row["label"])
        new_safe   = re.sub(r'[^\w\-]', '_', label)
        new_folder = BOXES_DIR / new_safe
        if old_folder.exists():
            old_folder.rename(new_folder)

    db.execute(
        "UPDATE boxes SET label=?, contents=?, ai_contents=?, location=?, label_size=?, label_orient=?, updated_at=datetime('now') WHERE id=?",
        (label, contents, ai_contents, location, label_size, label_orient, box_id)
    )
    loc = (location or "").strip()
    if loc:
        try:
            db.execute("INSERT INTO locations (name) VALUES (?)", (loc,))
        except sqlite3.IntegrityError:
            pass
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/boxes/<int:box_id>", methods=["DELETE"])
def delete_box(box_id):
    db = get_db()
    row = db.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    # Delete files
    folder = box_folder(row["label"])
    if folder.exists():
        import shutil
        shutil.rmtree(folder)

    db.execute("DELETE FROM boxes WHERE id=?", (box_id,))
    db.commit()
    return jsonify({"ok": True})


# ── Attachments ───────────────────────────────────────────────────────────────
@app.route("/api/boxes/<int:box_id>/attachments", methods=["POST"])
def upload_attachments(box_id):
    db = get_db()
    row = db.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not row:
        return jsonify({"error": "Box not found"}), 404

    existing = db.execute("SELECT COUNT(*) FROM attachments WHERE box_id=?", (box_id,)).fetchone()[0]
    files = request.files.getlist("files")

    if existing + len(files) > MAX_ATTACH:
        return jsonify({"error": f"Max {MAX_ATTACH} attachments per box"}), 400

    folder    = box_folder(row["label"])
    saved     = []
    ai_items  = []

    for f in files:
        if not f or not allowed_file(f.filename):
            continue
        orig   = secure_filename(f.filename)
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        ext    = orig.rsplit(".", 1)[-1].lower() if "." in orig else "bin"
        stored = f"{ts}.{ext}"
        dest   = folder / stored
        f.save(str(dest))

        cur = db.execute(
            "INSERT INTO attachments (box_id, filename, orig_name, mimetype, ai_desc) VALUES (?,?,?,?,?)",
            (box_id, stored, orig, f.mimetype or "", "")
        )
        saved.append({"id": cur.lastrowid, "filename": stored, "orig_name": orig, "ai_desc": ""})

    db.commit()
    return jsonify({"saved": saved, "message": f"{len(saved)} file(s) uploaded. Use 'Analyze with AI' to process images."})


@app.route("/api/attachments/<int:att_id>", methods=["DELETE"])
def delete_attachment(att_id):
    db = get_db()
    att = db.execute(
        "SELECT a.*, b.label FROM attachments a JOIN boxes b ON a.box_id=b.id WHERE a.id=?", (att_id,)
    ).fetchone()
    if not att:
        return jsonify({"error": "Not found"}), 404

    folder = box_folder(att["label"])
    dest   = folder / att["filename"]
    if dest.exists():
        dest.unlink()

    db.execute("DELETE FROM attachments WHERE id=?", (att_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/boxes/<int:box_id>/analyze_all", methods=["POST"])
def analyze_all_attachments(box_id):
    """Analyze all unanalyzed images for a box in a background thread."""
    db = get_db()
    row = db.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not row:
        return jsonify({"error": "Box not found"}), 404

    # Get all image attachments (analyzed or not — re-analyze all)
    atts = db.execute(
        "SELECT * FROM attachments WHERE box_id=?", (box_id,)
    ).fetchall()
    images = [dict(a) for a in atts if is_image(a["filename"])]
    label  = row["label"]

    if not images:
        return jsonify({"message": "No images to analyze", "count": 0})

    def run_analysis():
        results = []
        for att in images:
            folder = box_folder(label)
            path   = folder / att["filename"]
            if not path.exists():
                continue
            ai_desc = analyze_image_with_ollama(path)
            with get_db() as tdb:
                tdb.execute("UPDATE attachments SET ai_desc=? WHERE id=?", (ai_desc, att["id"]))
                if ai_desc and not ai_desc.startswith("["):
                    results.append(ai_desc)

        # Merge all descriptions into box ai_contents
        if results:
            merged = "\n".join(results)
            with get_db() as tdb:
                tdb.execute(
                    "UPDATE boxes SET ai_contents=?, updated_at=datetime('now') WHERE id=?",
                    (merged, box_id)
                )

    thread = threading.Thread(target=run_analysis, daemon=True)
    thread.start()
    return jsonify({"message": f"Analyzing {len(images)} image(s) in background…", "count": len(images)})


@app.route("/api/attachments/<int:att_id>/analyze", methods=["POST"])
def reanalyze_attachment(att_id):
    db = get_db()
    att = db.execute(
        "SELECT a.*, b.label FROM attachments a JOIN boxes b ON a.box_id=b.id WHERE a.id=?", (att_id,)
    ).fetchone()
    if not att:
        return jsonify({"error": "Not found"}), 404
    if not is_image(att["filename"]):
        return jsonify({"error": "Not an image"}), 400

    folder  = box_folder(att["label"])
    path    = folder / att["filename"]
    ai_desc = analyze_image_with_ollama(path)

    db.execute("UPDATE attachments SET ai_desc=? WHERE id=?", (ai_desc, att_id))

    # Refresh box ai_contents
    all_descs = db.execute(
        "SELECT ai_desc FROM attachments WHERE box_id=? AND ai_desc != ''", (att["box_id"],)
    ).fetchall()
    merged = "\n".join(d["ai_desc"] for d in all_descs if not d["ai_desc"].startswith("["))
    db.execute("UPDATE boxes SET ai_contents=?, updated_at=datetime('now') WHERE id=?", (merged, att["box_id"]))
    db.commit()

    return jsonify({"ai_desc": ai_desc})


# ── Static file serving for attachments ──────────────────────────────────────
@app.route("/files/<path:filepath>")
def serve_file(filepath):
    return send_from_directory(BOXES_DIR, filepath)


# ── Printable list ────────────────────────────────────────────────────────────
@app.route("/print")
def print_view():
    location = request.args.get("location", "").strip()
    db = get_db()
    if location and location != "__all__":
        rows = db.execute(
            "SELECT * FROM boxes WHERE location = ? COLLATE NOCASE", (location,)
        ).fetchall()
        title = location
    else:
        rows = db.execute("SELECT * FROM boxes").fetchall()
        title = "All Locations"

    rows = sort_boxes_by_location(rows)

    boxes = []
    for row in rows:
        box = row_to_dict(row)
        atts = db.execute(
            "SELECT * FROM attachments WHERE box_id=? ORDER BY created_at", (box["id"],)
        ).fetchall()
        box["attachments"] = [row_to_dict(a) for a in atts]
        box["folder"] = re.sub(r'[^\w\-]', '_', box["label"])
        boxes.append(box)

    return render_template(
        "print.html",
        boxes=boxes,
        title=title,
        generated=datetime.now().strftime("%B %-d, %Y %-I:%M %p"),
        is_image=is_image,
    )


# ── QR codes & labels ─────────────────────────────────────────────────────────
def _get_box_dict(box_id):
    row = get_db().execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    return row_to_dict(row) if row else None


@app.route("/qr/<int:box_id>.svg")
def qr_code(box_id):
    """QR code linking to this tote by bin number. Not cached long: renaming the bin number changes it."""
    box = _get_box_dict(box_id)
    if not box:
        return "Not found", 404
    return Response(labels.qr_svg(labels.box_url(box["label"])), mimetype="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})


def _label_opts(box):
    size = labels.normalize_size(request.args.get("size") or box.get("label_size"))
    orient = labels.normalize_orient(request.args.get("orient") or box.get("label_orient"))
    return size, orient


@app.route("/label/<int:box_id>")
def label_preview(box_id):
    """On-screen label preview (the exact rendered label) with a browser-print button."""
    box = _get_box_dict(box_id)
    if not box:
        return "Tote not found", 404
    size, orient = _label_opts(box)
    w, h = labels.design_dims(size, orient)
    return render_template("label.html", box=box, size=size, orient=orient,
                           ref=quote(labels.bin_ref(box["label"]), safe=""),
                           sizes=labels.LABEL_SIZES, orients=labels.ORIENTS,
                           w_in=w / 72, h_in=h / 72)


@app.route("/api/boxes/<int:box_id>/label.png")
def label_png(box_id):
    box = _get_box_dict(box_id)
    if not box:
        return jsonify({"error": "Not found"}), 404
    size, orient = _label_opts(box)
    png = labels.render_label_png(labels.render_label_pdf(box, size, orient), dpi=200)
    return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.route("/api/boxes/<int:box_id>/label.pdf")
def label_pdf(box_id):
    box = _get_box_dict(box_id)
    if not box:
        return jsonify({"error": "Not found"}), 404
    size, orient = _label_opts(box)
    pdf = labels.render_label_pdf(box, size, orient)
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="label-{box_id}-{size}-{orient}.pdf"'})


@app.route("/api/printers")
def api_printers():
    return jsonify(labels.list_printers())


@app.route("/api/boxes/<int:box_id>/print-label", methods=["POST"])
def print_label_api(box_id):
    box = _get_box_dict(box_id)
    if not box:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    printer = (data.get("printer") or "").strip()
    if not printer:
        return jsonify({"error": "No printer selected"}), 400
    size = labels.normalize_size(data.get("size") or box.get("label_size"))
    orient = labels.normalize_orient(data.get("orient") or box.get("label_orient"))
    try:
        copies = int(data.get("copies") or 1)
    except (TypeError, ValueError):
        copies = 1
    pdf = labels.render_label_pdf(box, size, orient)
    ok, msg, warning = labels.print_label(pdf, printer, size, orient, copies)
    return jsonify({"ok": ok, "message": msg, "warning": warning}), (200 if ok else 502)


# ── Ollama status ─────────────────────────────────────────────────────────────
@app.route("/api/ollama/status")
def ollama_status():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", headers=ollama_headers(), timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        vision = OLLAMA_MODEL in models or any(OLLAMA_MODEL.split(":")[0] in m for m in models)
        return jsonify({"ok": True, "models": models, "vision_ready": vision})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(f"""
╔══════════════════════════════════════════════════╗
║          ToteTracker is starting up!             ║
║  Open in browser: http://0.0.0.0:{PORT}           ║
║  Data stored in:  {DATA_DIR}
║  Ollama endpoint: {OLLAMA_URL}                    ║
╚══════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=PORT, debug=False)
