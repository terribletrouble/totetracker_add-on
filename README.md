# Jason's Home Assistant Add-ons

## ToteTracker
Storage tote inventory with photos, AI-generated contents (Ollama), QR codes, and direct label printing
to a TSPL thermal label printer. Runs as a Home Assistant add-on with a sidebar panel; its data is included
in Home Assistant backups.

### Install
1. Home Assistant → **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/terribletrouble/totetracker_add-on`
3. Open **ToteTracker**, **Install**, review the **Configuration** tab (see `totetracker/DOCS.md`), **Start**,
   and enable **Show in sidebar**.

Your own hostnames, printer address, and Ollama URL are entered in the add-on's Configuration tab — they are
stored in Home Assistant, not in this repository.

### Updating
Edit the code, **bump `version` in `totetracker/config.yaml`**, commit and push. In Home Assistant:
Add-on Store → ⋮ → **Check for updates** → ToteTracker → **Update**. (HA only offers an update when the
version number changes.) Add a line to `totetracker/CHANGELOG.md` for each release.

### Developing locally
```bash
sudo apt install poppler-utils            # label rasterizing
cd totetracker
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data BASE_URL=http://localhost:5000 python app.py     # http://localhost:5000
```
Optional: `LABEL_PRINTERS="My Printer=PRINTER_IP:9100"`, `OLLAMA_URL=http://OLLAMA_HOST:11434`.
Never commit `data/` (your inventory and photos) — it is in `.gitignore`.
