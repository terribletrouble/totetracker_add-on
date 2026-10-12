# ToteTracker

Storage tote inventory: photos, AI-generated contents (Ollama), QR codes, and label printing to a TSPL
thermal printer.

## Using it
- **Inside Home Assistant:** the **ToteTracker** entry in the sidebar (HA's own HTTPS — no mixed-content problems).
- **Directly:** `http://<ha-host>:5000`. QR codes on printed labels use the **Base URL** option
  (set it to a name or IP that resolves to this Home Assistant host, e.g. `http://homeassistant.local:5000`), so that name must resolve to this Home Assistant host.

## Options
| Option | Meaning |
|---|---|
| `base_url` | Address encoded in every QR code. Printed labels bake it in — choose one you'll keep. |
| `label_printers` | `Name=host:port` list, port 9100 for raw TSPL. Use an **IP** if `.local` names don't resolve inside add-ons. |
| `label_dpi` | Printer resolution (203 typical, 300 if yours is). |
| `label_rotate` | `90`, or `270` if landscape labels print upside-down. |
| `tspl_gap`, `tspl_density`, `tspl_invert` | Printer tuning (gap in inches, darkness 0–15, invert only if labels print white-on-black). |
| `ollama_url`, `ollama_model`, `ollama_api_key` | Photo analysis backend. |

## Backups
Everything (database + photos) lives in the add-on's data volume, so it is included whenever the add-on is
selected in a Home Assistant backup. The add-on is stopped briefly during the backup (`backup: cold`)
so the database is copied consistently.

## Importing an existing ToteTracker
Copy the contents of the old data folder (`totetracker.db` and the `boxes/` folder) to
`share/totetracker_import/` before the **first** start. It is imported only if the add-on has no
database yet. Delete the import folder afterwards.
