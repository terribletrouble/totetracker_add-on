"""
ToteTracker label helpers: QR codes, PDF label rendering, and CUPS printing.

Labels are rendered as vector PDFs at the exact physical label size, so the
printer driver (CUPS) only has to rasterize at the printer's native DPI.
"""

import io
import os
import re
import socket
import subprocess
import tempfile
from urllib.parse import quote

import segno
from reportlab.lib.units import inch
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

# size key -> physical STOCK (width_pt, height_pt, human name) as it feeds through the printer.
# 4x6 stock is portrait (4 wide, 6 tall); 3x2 stock is landscape (3 wide, 2 tall).
LABEL_SIZES = {
    "4x6": (4 * inch, 6 * inch, "Large (4 × 6 in)"),
    "3x2": (3 * inch, 2 * inch, "Small (3 × 2 in)"),
}
DEFAULT_SIZE = "4x6"

ORIENTS = {"landscape": "Landscape", "portrait": "Portrait"}
DEFAULT_ORIENT = "landscape"

# Base URL encoded into the QR code. Override with BASE_URL in docker-compose.
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")

# Printer resolution in dots/inch. 203 is typical; set LABEL_DPI=300 if yours is 300.
LABEL_DPI = int(os.environ.get("LABEL_DPI", "203"))
# When the design orientation differs from how the stock feeds, the bitmap is rotated.
# 90 = counter-clockwise, 270 = clockwise. Flip this if landscape labels print upside down.
LABEL_ROTATE = int(os.environ.get("LABEL_ROTATE", "90"))

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"


def normalize_size(size):
    size = (size or "").strip().lower()
    return size if size in LABEL_SIZES else DEFAULT_SIZE


def normalize_orient(orient):
    orient = (orient or "").strip().lower()
    return orient if orient in ORIENTS else DEFAULT_ORIENT


def design_dims(size_key, orient):
    """(width_pt, height_pt) of the label as the user reads it."""
    w, h, _ = LABEL_SIZES[normalize_size(size_key)]
    long_, short = max(w, h), min(w, h)
    return (long_, short) if normalize_orient(orient) == "landscape" else (short, long_)


def bin_ref(label):
    """The bin number a tote is known by: the part of its name before ' - ' (e.g. '217' for
    '217 - Electrical Switch Plates'). Names without that shape use the whole name."""
    return split_label((label or "").strip())[0]


def box_url(label):
    """URL encoded in the QR code: BASE_URL/<bin number>."""
    return f"{BASE_URL}/{quote(bin_ref(label), safe='')}"


# ── QR ────────────────────────────────────────────────────────────────────────
def qr_svg(url, scale=8):
    """QR code as an SVG byte string (white background, 1-module quiet zone)."""
    q = segno.make_qr(url, error="m")
    buf = io.BytesIO()
    q.save(buf, kind="svg", scale=scale, border=1, dark="#000000", light="#ffffff",
           xmldecl=False, nl=False)
    return buf.getvalue()


def qr_size_snapped(url, target_pt, dpi=None):
    """Largest QR size <= target_pt in which every module is a whole number of printer dots.
    Whole-dot modules print crisp on a thermal head; fractional ones blur and scan poorly."""
    dpi = dpi or LABEL_DPI
    dot = 72.0 / dpi
    n = len(list(segno.make_qr(url, error="m").matrix_iter(border=1)))
    mod_dots = max(1, int((target_pt / dot) // n))
    return n * mod_dots * dot


def _draw_qr(c, url, x, y, size, dpi=None):
    """Draw a QR code as vector rectangles, bottom-left at (x, y), `size` pt square.
    Horizontal runs of dark modules are merged so rasterizing never leaves hairline gaps.
    Origin is snapped to the printer's dot grid."""
    dpi = dpi or LABEL_DPI
    dot = 72.0 / dpi
    x = round(x / dot) * dot
    y = round(y / dot) * dot
    q = segno.make_qr(url, error="m")
    border = 1
    rows = [list(r) for r in q.matrix_iter(border=border)]
    n = len(rows)
    mod = size / n
    c.setFillColorRGB(0, 0, 0)
    for r, row in enumerate(rows):
        col = 0
        while col < n:
            if row[col]:
                start = col
                while col < n and row[col]:
                    col += 1
                top = y + size - r * mod
                c.rect(x + start * mod, top - mod, (col - start) * mod, mod, stroke=0, fill=1)
            else:
                col += 1


# ── Text helpers ──────────────────────────────────────────────────────────────
def _clean(text):
    """Helvetica (built-in PDF font) only covers Latin-1/cp1252 — swap anything else for '?'."""
    return (text or "").encode("cp1252", "replace").decode("cp1252").replace("\r", "")


def _fit_single_line(text, font, max_size, min_size, width):
    size = max_size
    while size > min_size and stringWidth(text, font, size) > width:
        size -= 1
    # Still too wide at min size: trim with an ellipsis.
    if stringWidth(text, font, size) > width:
        while text and stringWidth(text + "...", font, size) > width:
            text = text[:-1]
        text = text.rstrip() + "..."
    return text, size


def _fit_block(text, font, max_size, min_size, width, max_height, leading=1.18):
    """Largest font size where wrapped text fits max_height; ellipsize at min size."""
    size = max_size
    while True:
        lines = simpleSplit(text, font, size, width)
        if len(lines) * size * leading <= max_height or size <= min_size:
            break
        size -= 1
    max_lines = max(1, int(max_height // (size * leading)))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and stringWidth(last + "...", font, size) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "..."
    return lines, size, size * leading


def split_label(label):
    """Split a tote name like '269 - Electrical Switch Plates' into ('269', 'Electrical Switch Plates').
    Names without a leading number/dash ('Box-17', 'Holiday Decor') stay whole as the big line."""
    label = (label or "").strip()
    m = re.match(r"^(.{1,12}?)\s+[-\u2013\u2014:]\s+(.+)$", label)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(\d+[A-Za-z]?)\s+(.+)$", label)
    if m:
        return m.group(1), m.group(2).strip()
    return label, ""


def _description(box):
    """Contents text for the label: the manual Contents field (AI text only as a fallback)."""
    text = (box.get("contents") or "").strip()
    if not text:
        text = (box.get("ai_contents") or "").strip()
    return _clean(text)


# ── PDF label ─────────────────────────────────────────────────────────────────
def render_label_pdf(box, size_key=None, orient=None):
    """Return PDF bytes for one tote label, page sized to the label as read (orientation applied)."""
    size_key = normalize_size(size_key or box.get("label_size"))
    orient = normalize_orient(orient or box.get("label_orient"))
    w, h = design_dims(size_key, orient)
    number, name = split_label(_clean(str(box["label"])))
    location = _clean((box.get("location") or "").strip())
    contents = _description(box)
    url = box_url(box["label"])

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    c.setTitle(f"ToteTracker label {number}")
    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)
    _layout(c, w, h, number, name, location, contents, url)
    c.showPage()
    c.save()
    return buf.getvalue()


def _flow(text, font, max_size, min_size, top, bottom, qr_top, wide, narrow, leading=1.15):
    """Greedy word-wrap that uses `wide` width above the QR and `narrow` beside it.
    Returns ([(line, baseline_y)], size). Ellipsizes only if it can't fit even at min_size."""
    words = text.split()
    size = max_size
    while True:
        lines, i, k, ok = [], 0, 0, True
        while i < len(words):
            base = top - size * 0.85 - k * size * leading
            if base - size * 0.2 < bottom:
                ok = False
                break
            width = wide if (base - size * 0.25) > qr_top + 3 else narrow
            word = words[i]
            if stringWidth(word, font, size) > width:       # a single word wider than the line: hard-split it
                cut = len(word)
                while cut > 1 and stringWidth(word[:cut], font, size) > width:
                    cut -= 1
                words[i:i + 1] = [word[:cut], word[cut:]]
                word = words[i]
            line = word
            i += 1
            while i < len(words) and stringWidth(line + " " + words[i], font, size) <= width:
                line += " " + words[i]
                i += 1
            lines.append((line, base))
            k += 1
        if ok or size <= min_size:
            break
        size -= 0.5
    if not ok and lines:
        last, base = lines[-1]
        width = wide if (base - size * 0.25) > qr_top + 3 else narrow
        while last and stringWidth(last + "...", font, size) > width:
            last = last[:-1]
        lines[-1] = (last.rstrip() + "...", base)
    return lines, size


def _layout(c, w, h, number, name, location, contents, url):
    """Priority: number > name > location > contents. QR bottom-right, URL caption bottom-left.
    Text uses the full width above the QR and wraps narrower beside it."""
    landscape = w > h
    short = min(w, h)
    big = short > 200                      # 4x6; the 3x2 has no room for a caption (QR only)
    m = max(9.0, short * 0.0625)
    gap = 8.0
    inner_w = w - 2 * m
    qr = qr_size_snapped(url, min(1.45 * inch, short * 0.44))
    qr_top = m + qr
    narrow = inner_w - qr - gap

    # 1) Number: as big as fits on one line; if it can't be big, wrap it rather than truncate.
    num_max = short * (0.34 if landscape else (0.42 if big else 0.36))
    if stringWidth(number, FONT_BOLD, max(18, num_max * 0.4)) <= inner_w:
        text, nsize = _fit_single_line(number, FONT_BOLD, num_max, 18, inner_w)
        nlines, nlead = [text], nsize * 0.95
    else:
        nlines, nsize, nlead = _fit_block(number, FONT_BOLD, num_max * 0.5, 12, inner_w, h * 0.32, leading=1.05)
    y = h - m - nsize * 0.8
    c.setFont(FONT_BOLD, nsize)
    for ln in nlines:
        c.drawString(m, y, ln)
        y -= nlead
    cursor = y + nlead - nsize * 0.25 - gap

    # Lowest point text may reach: above the caption/QR strip on a big portrait label,
    # above the caption on a big landscape label, else the bottom margin.
    if big and not landscape:
        bottom = m + qr + gap
    elif big:
        bottom = m + 26
    else:
        bottom = m
    avail = cursor - bottom

    def plan(name_max):
        """Lay out name, location, rule and contents (no drawing). Returns (ops, contents_fit)."""
        ops, cur, name_size = [], cursor, None
        if name:
            lines, name_size = _flow(name, FONT_BOLD, name_max, 9, cur,
                                     max(bottom, cur - avail * 0.5), qr_top, inner_w, narrow, 1.12)
            ops += [("text", FONT_BOLD, name_size, m, by, ln) for ln, by in lines]
            if lines:
                cur = lines[-1][1] - name_size * 0.2 - gap * 0.6
        if location:
            lmax = short * 0.075
            guess = cur - lmax * 0.85
            width = inner_w if (guess - lmax * 0.25) > qr_top + 3 else narrow
            loc, lsize = _fit_single_line(location.upper(), FONT_BOLD, lmax, 7, width)
            ops.append(("text", FONT_BOLD, lsize, m, cur - lsize * 0.85, loc))
            cur -= lsize * 1.15 + gap * 0.5
        if name or location:
            ops.append(("rule", m, cur, m + (inner_w if cur > qr_top + 3 else narrow)))
            cur -= gap * 0.8
        fit = True
        if contents:
            fit = cur - bottom > 10
            if fit:
                cmax = short * 0.085
                if name_size:
                    cmax = min(cmax, name_size * 0.8)
                lines, csize = _flow(contents, FONT, cmax, 6, cur, bottom, qr_top, inner_w, narrow, 1.15)
                ops += [("text", FONT, csize, m, by, ln) for ln, by in lines]
                fit = csize >= 6.5 and not lines[-1][0].endswith("...") or len(lines) >= 1 and csize >= 7
        return ops, fit

    # Prefer a big name, but shrink it (down to 9pt) if that's what it takes to also fit the contents.
    name_max = short * 0.12
    while True:
        ops, fit = plan(name_max)
        if fit or not name or name_max <= 9:
            break
        name_max -= 1
    c.setLineWidth(1.5 if big else 1)
    for op in ops:
        if op[0] == "text":
            _, font, size, x, by, txt = op
            c.setFont(font, size)
            c.drawString(x, by, txt)
        else:
            _, x1, yy, x2 = op
            c.line(x1, yy, x2, yy)

    # 5) QR bottom-right (+ caption and URL bottom-left on the large label)
    _draw_qr(c, url, w - m - qr, m, qr)
    if big:
        c.setFont(FONT_BOLD, 9)
        if landscape:
            c.drawString(m, m + 11, "SCAN FOR FULL CONTENTS")
            c.setFont(FONT, 8)
            c.drawString(m, m, url)
        else:
            c.drawString(m, m + qr - 9, "SCAN FOR FULL CONTENTS")
            c.setFont(FONT, 8)
            c.drawString(m, m + qr - 22, url)


# ── Printing: TSPL over raw TCP (port 9100) ───────────────────────────────────
# LABEL_PRINTERS = "Name=host:port,Name2=host2:port"  (port defaults to 9100)
DEFAULT_PRINTERS = ""   # set LABEL_PRINTERS="Name=host:port" (raw TSPL port, usually 9100)


def configured_printers():
    raw = os.environ.get("LABEL_PRINTERS", "").strip() or DEFAULT_PRINTERS
    out = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, addr = part.partition("=")
        if not sep:
            name, addr = part, part
        host, _, port = addr.strip().partition(":")
        out[name.strip()] = (host.strip(), int(port or 9100))
    return out


def list_printers():
    printers = configured_printers()
    names = list(printers)
    default = os.environ.get("DEFAULT_PRINTER", "").strip()
    if default not in printers:
        default = names[0] if names else None
    return {"printers": names, "default": default, "error": None if names else "No printers configured"}


def rasterize_label(pdf_bytes, size_key, orient=None, dpi=None):
    """PDF -> 1-bit PIL image at the printer's native resolution, in the STOCK's orientation
    (rotated if the design is landscape on portrait stock, or vice versa)."""
    from PIL import Image
    dpi = dpi or LABEL_DPI
    dw_pt, dh_pt = design_dims(size_key, orient)
    d_px = (round(dw_pt / 72 * dpi), round(dh_pt / 72 * dpi))
    sw_pt, sh_pt, _ = LABEL_SIZES[normalize_size(size_key)]
    s_px = (round(sw_pt / 72 * dpi), round(sh_pt / 72 * dpi))
    with tempfile.TemporaryDirectory() as td:
        pdf_path = os.path.join(td, "label.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        r = subprocess.run(
            ["pdftoppm", "-r", str(dpi), "-gray", "-png", "-singlefile", pdf_path, os.path.join(td, "out")],
            capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError("pdftoppm failed: " + r.stderr.decode(errors="replace"))
        img = Image.open(os.path.join(td, "out.png")).convert("L")
        img.load()
    if img.size != d_px:
        img = img.resize(d_px)
    if d_px != s_px:
        img = img.rotate(LABEL_ROTATE, expand=True)
        if img.size != s_px:
            img = img.resize(s_px)
    return img.point(lambda v: 255 if v >= 128 else 0).convert("1")


def render_label_png(pdf_bytes, dpi=150):
    """PNG bytes of a label PDF (used for the on-screen preview — identical to what prints)."""
    with tempfile.TemporaryDirectory() as td:
        pdf_path = os.path.join(td, "label.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        r = subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", pdf_path,
                            os.path.join(td, "out")], capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError("pdftoppm failed: " + r.stderr.decode(errors="replace"))
        with open(os.path.join(td, "out.png"), "rb") as f:
            return f.read()


def build_tspl(img, size_key, copies=1):
    """TSPL job: SIZE/GAP/CLS + one full-label BITMAP + PRINT."""
    from PIL import Image
    size_key = normalize_size(size_key)
    w_in, h_in = int(LABEL_SIZES[size_key][0] / 72), int(LABEL_SIZES[size_key][1] / 72)
    w_px, h_px = img.size
    wbytes = (w_px + 7) // 8

    # Pillow mode "1": 1 = white. TSPL BITMAP mode 0: bit 0 = black on most TSC-compatible
    # printers, so packed Pillow data is already in the right polarity. TSPL_INVERT=1 flips it.
    if os.environ.get("TSPL_INVERT", "0") == "1":
        from PIL import ImageOps
        img = ImageOps.invert(img.convert("L")).convert("1")
    padded = Image.new("1", (wbytes * 8, h_px), 1)
    padded.paste(img, (0, 0))
    data = padded.tobytes()  # rows are byte-aligned, MSB first

    gap = os.environ.get(f"TSPL_GAP_{size_key.upper()}", os.environ.get("TSPL_GAP", "0.12,0"))
    head = [f"SIZE {w_in},{h_in}", f"GAP {gap}"]
    if os.environ.get("TSPL_DENSITY"):
        head.append(f"DENSITY {int(os.environ['TSPL_DENSITY'])}")
    if os.environ.get("TSPL_SPEED"):
        head.append(f"SPEED {os.environ['TSPL_SPEED']}")
    if os.environ.get("TSPL_DIRECTION"):
        head.append(f"DIRECTION {os.environ['TSPL_DIRECTION']}")
    head += ["CLS", f"BITMAP 0,0,{wbytes},{h_px},0,"]
    tail = f"\r\nPRINT 1,{max(1, int(copies))}\r\n"
    return ("\r\n".join(head)).encode("ascii") + data + tail.encode("ascii")


def print_label(pdf_bytes, printer, size_key, orient=None, copies=1):
    """Rasterize the label and send it to the printer as TSPL.
    Returns (ok, message, warning). The printer's raw reply is always logged (docker compose logs)."""
    printers = configured_printers()
    if printer not in printers:
        return False, f"Unknown printer '{printer}'", None
    host, port = printers[printer]
    copies = max(1, min(int(copies or 1), 20))
    try:
        job = build_tspl(rasterize_label(pdf_bytes, size_key, orient), size_key, copies)
    except Exception as e:
        return False, f"Could not render label: {e}", None
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(job)
            sock.settimeout(1.5)
            reply = b""
            try:
                while len(reply) < 512:
                    chunk = sock.recv(256)
                    if not chunk:
                        break
                    reply += chunk
            except socket.timeout:
                pass
    except OSError as e:
        print(f"[PRINT] connect/send to {host}:{port} failed: {e}", flush=True)
        return False, f"Could not reach {host}:{port} — {e}", None
    text = reply.decode(errors="replace").strip()
    print(f"[PRINT] {printer} {size_key}/{orient} x{copies} ({len(job)} bytes) reply={text!r}", flush=True)
    if "error" in text.lower():
        # The printer prints fine in many "error" cases (e.g. an ignored setup command), so report
        # it as a warning unless the printer never acknowledged the job.
        acked = "done" in text.lower() or "printing" in text.lower()
        if acked:
            return True, f"Sent {copies} label(s) to {printer}", f"Printer replied: {text}"
        return False, f"Printer reported: {text}", None
    return True, f"Sent {copies} label(s) to {printer}", None
