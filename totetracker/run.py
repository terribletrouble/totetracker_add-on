"""Add-on entrypoint: map HA add-on options -> environment, import data once, start ToteTracker."""
import json
import os
import shutil
import sys
from pathlib import Path

ADDON_DATA = Path(os.environ.get("ADDON_DATA", "/data"))        # persistent, included in HA backups
SHARE = Path(os.environ.get("ADDON_SHARE", "/share"))            # for the one-time import
IMPORT_DIR = SHARE / "totetracker_import"

OPTION_ENV = {
    "base_url": "BASE_URL",
    "label_printers": "LABEL_PRINTERS",
    "label_dpi": "LABEL_DPI",
    "label_rotate": "LABEL_ROTATE",
    "tspl_gap": "TSPL_GAP",
    "tspl_density": "TSPL_DENSITY",
    "ollama_url": "OLLAMA_URL",
    "ollama_model": "OLLAMA_MODEL",
    "ollama_api_key": "OLLAMA_API_KEY",
}


def prepare():
    opts_file = ADDON_DATA / "options.json"
    opts = json.loads(opts_file.read_text()) if opts_file.exists() else {}
    for key, env in OPTION_ENV.items():
        val = opts.get(key)
        if val is not None and str(val) != "":
            os.environ[env] = str(val)
    if opts.get("tspl_invert"):
        os.environ["TSPL_INVERT"] = "1"

    data_dir = ADDON_DATA / "totetracker"
    os.environ["DATA_DIR"] = str(data_dir)

    # One-time import of an existing data folder (totetracker.db + boxes/) dropped into
    # <share>/totetracker_import/. Only happens when there is no database yet, so it can
    # never overwrite live data.
    if not (data_dir / "totetracker.db").exists() and (IMPORT_DIR / "totetracker.db").exists():
        print(f"[ADDON] Importing existing data from {IMPORT_DIR}", flush=True)
        shutil.copytree(IMPORT_DIR, data_dir, dirs_exist_ok=True)
        print("[ADDON] Import complete — you can delete the totetracker_import folder from the share.", flush=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


if __name__ == "__main__":
    prepare()
    os.execvp(sys.executable, [sys.executable, "app.py"])
