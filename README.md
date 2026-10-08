# Spooler v2

Local web GUI for **Elegoo Centauri Carbon** FDM 3D printers (CC1 and CC2). Monitor and control multiple printers from a single browser tab — or install as a PWA on your phone. Experimental support for **Prusa (PrusaLink)** and **Klipper (Moonraker)** printers is also included.

![Status: printing, paused, idle, complete](https://img.shields.io/badge/CC1%20%26%20CC2-supported-brightgreen)
![Prusa](https://img.shields.io/badge/Prusa%20(PrusaLink)-experimental-orange)
![Klipper](https://img.shields.io/badge/Klipper%20(Moonraker)-experimental-orange)

> **Disclaimer.** Spooler is an independent project and is not affiliated with, endorsed by, or supported by ELEGOO, Prusa Research, or the Klipper/Moonraker projects. ELEGOO, Centauri Carbon, Prusa, PrusaLink, Klipper and Moonraker are trademarks or names of their respective owners and are used here only to say which printers Spooler works with.

> **Branches**
> - `main` — latest stable release. This is what the Docker image (`ghcr.io/tharje/spooler-v2:latest`) is built from.
> - `dev` — active development. New features and fixes land here first and are tested before being merged to `main`.

## Quick start

**Requires:** [Docker](https://docs.docker.com/get-docker/)

```bash
curl -fsSL https://raw.githubusercontent.com/tharje/spooler-v2/main/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

Open **`http://<server-ip>:8080`** in your browser. On first visit you will be prompted to create a username and password.

> **Note:** `network_mode: host` is required on Linux. On **macOS or Windows**, Docker's host networking does not work — use the [Python setup](#option-b--python-no-docker) instead.

---

## Features

- **Live status** – idle, preparing, printing, paused, complete, cancelled, error – with what the printer is doing while it prepares (heating nozzle/bed, homing, leveling, loading filament …) and the cause and error code when a print pauses or fails
- **Camera feed** – MJPEG stream from each printer, proxied through Spooler behind auth
- **Temperature monitoring** – nozzle, bed and chamber with live target values
- **Print progress** – layer count (`Layer 25 / 120`), percentage, elapsed and remaining time
- **Print speed control** – adjust speed while printing or paused (CC1 and CC2)
- **Print options** – preview thumbnail, build plate, time-lapse and bed levelling before starting a print
- **File browser and upload** – list, upload, start and delete files stored on the printer
- **Canvas / AMS multi-material** – shows filament slots; link each slot to a Spoolman spool for per-tray deduction
- **Filament tracking** – live mm/g per print; deducts from the correct spool on tray changes
- **Stats & history** – statistics per period and printer, every past print with the picture taken when it ended, its details and a reference number you can put on it; CSV export
- **Notifications** – browser push, ntfy, Telegram, Discord or any webhook, for print started/complete/cancelled/paused/error, filament runout, printer offline/online, hot nozzle, layer checkpoint and low spool, optionally with a camera picture
- **Automatic light** – per printer (Centauri Carbon): light on when a print starts, off when it ends
- **External API** – let other programs (for example a CRM) read the print history and pictures and set reference numbers, using API keys ([docs/external-api.md](docs/external-api.md))
- **Controls** – pause, resume, stop, light toggle
- **Auto-discovery** – finds printers on the local network automatically
- **Persistence** – printers, history and spool data saved between restarts
- **Authentication** – password-protected login; first-time setup via the web UI, no terminal needed
- **Spoolman integration** – filament spool inventory with auto-deduction after each print
- **Filament catalogue import** – import a brand's filament types (Elegoo and others) from SpoolmanDB
- **Feature switches** – turn camera, notifications, backups, Spoolman, upload, statistics and more on or off under Settings → Features
- **PWA** – installable as a home screen app on Android and iOS
- **HTTPS** – self-signed cert on port 8443; works with Tailscale serve out of the box

> Some of this is new and still in testing on real hardware — notably the notification channels, Centauri Carbon 2 file upload, the status details for the Centauri Carbon 1, and the print pictures. If something misbehaves, use **Report bug** in the footer.

---

## Installation

### Option A – Docker (recommended)

No git clone needed. Just download the compose file and start:

```bash
curl -fsSL https://raw.githubusercontent.com/tharje/spooler-v2/main/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

Open **`http://<server-ip>:8080`**.

Both Spooler and Spoolman start automatically. Data is stored in Docker volumes (`spooler_data`, `spoolman_data`) and survives restarts and rebuilds.

**Useful commands:**

```bash
docker compose pull && docker compose up -d   # update to latest version
docker compose logs -f                        # live logs
docker compose down                           # stop (data preserved)
```

### Option B – Python (no Docker)

```bash
git clone https://github.com/tharje/spooler-v2.git
cd spooler-v2
./setup.sh
```

Spoolman needs to run separately — see [Spoolman docs](https://github.com/Donkie/Spoolman).

---

## Install as PWA

**Android** (Chrome): open the URL → tap ⋮ → **Add to Home Screen**  
**iOS** (Safari): open the URL → tap Share → **Add to Home Screen**

For a proper standalone app (no browser chrome), use HTTPS:

- `https://<server-ip>:8443` — self-signed cert (download and install it from that URL)
- [Tailscale serve](https://tailscale.com/kb/1312/serve) — valid cert automatically

---

## Authentication

On first visit, Spooler shows a **Create account** page. Choose a username and password — done. No terminal or config file needed.

### Disable authentication

For trusted local networks where you don't want a password, create a `.env` file next to `docker-compose.yml`:

```env
AUTH_ENABLED=false
```

Then restart: `docker compose up -d`

### Advanced: credentials via environment variable

If you prefer to manage credentials outside the data volume (e.g. Docker secrets):

```bash
# Generate a bcrypt hash
docker compose exec spooler python3 server.py --hash-password
```

Add to `.env`:

```env
SPOOLER_PW_HASH=$2b$12$...
SPOOLER_USERNAME=admin
```

See [`.env.example`](.env.example) for all options.

---

## Adding printers

- Click **Discover** to auto-find printers on the network
- Click **Add Printer** to enter an IP address manually

For **CC2 (Centauri Carbon 2)**, you also need the MQTT password shown on the printer under **Settings → Network**.

For **Prusa printers (PrusaLink)**, select *Prusa – PrusaLink HTTP API* and enter the API key from your printer's PrusaLink settings. *(Still experimental — basic control and file browser work, some features may be limited.)*

For **Klipper printers (Moonraker)**, select *Klipper – Moonraker HTTP API* and enter the IP address of your Moonraker host (default port 7125). API key is optional — only needed if your Moonraker is configured with authentication. *(Still experimental — basic control and file browser work, some features may be limited.)*

Printer configs are saved and reconnect automatically on restart.

---

## Filament tracking

- Live filament usage (mm and grams) shown on each printer card while printing
- All completed, cancelled and failed prints are logged and shown under **Stats → Prints**
- **Single-material printers** – assign a Spoolman spool to the printer; used grams are deducted automatically after each print
- **Multi-material (Canvas/AMS)** – link each filament slot to a Spoolman spool; filament is tracked per tray and deducted from the correct spool when the active tray changes

---

## Backup & restore

Settings → **Backup** lets you download a zip of your printers, history, spool-tray links and notification settings, and restore from one later.

- Printer access codes (CC2 MQTT password, Moonraker/PrusaLink API key) are left out of the download by default — check **Include secrets** if you want them included.
- Restoring overwrites current data with the backup's contents. A safety copy of whatever was there before is taken automatically first, in case you need to undo it.
- Spooler **restarts immediately** after a successful restore to apply it everywhere — your browser will reconnect on its own after a few seconds.
- Automatic backups are taken daily and before version upgrades, kept in `DATA_DIR/backups/` (last 7 by default — see `.env.example` for `SPOOLER_AUTO_BACKUP_DAILY` / `SPOOLER_BACKUP_KEEP`), and listed in the same Settings page for download.

---

## File upload

Open a printer's file browser and use **Upload** (or drop a `.gcode`/`.gco`/`.bgcode` file on the box) to send a file to the printer, optionally starting the print right away. Supported on Elegoo CC1 and CC2, Moonraker and PrusaLink printers (CC2 upload follows the same method Elegoo's own slicer uses — chunked `PUT /upload` on port 80 — and is verified on a real CC2; Moonraker/PrusaLink are not yet verified on real printers). Files are streamed through `DATA_DIR/uploads/` (never held in memory) and deleted once sent; leftovers older than 24 h are removed at startup. The size limit is `SPOOLER_MAX_UPLOAD_MB` (default 500). Can be switched off under Settings → Features (`file_upload`).

## Stats & history

The **Stats** button in the side menu opens two tabs:

- **Overview** – prints, success rate, print time and filament for the last 7 days, 30 days, 12 months, all time or a custom period (for all printers or one), a chart per day/week/month (with a table view), filament by material and printer, results and why prints stopped.
- **Prints** – every print, newest first, searchable by file name or reference. Open one to see the picture taken when it ended, its result and cause, start/end times, filament, material, spools used and pauses, and give it a **reference number** (for example an order number).

**Export CSV** downloads the history for the chosen period (semicolon-separated, UTF-8 with a byte-order mark so it opens in Excel).

A camera picture is saved with each finished, cancelled or failed print (about 50–150 KB each) and removed together with its history entry; switch pictures off under Settings → Features (`print_snapshot`).

## Notifications

Settings → **Notifications**. Under **Notification channels** switch on the places to send to and fill in their details; each has a **Send test** button:

- **Browser push** – on this device, also when the app is closed
- **ntfy** – ntfy.sh or your own server (topic, optional access token)
- **Telegram** – your own bot (bot token and chat ID)
- **Discord** – a channel webhook
- **Webhook** – every notification as JSON (`POST`) to an address you choose

Then choose which events to be told about; started, paused, error, filament runout and complete can include a camera picture. The same event is sent at most once per printer every 10 minutes (finished and failed prints always are). Tokens and webhook addresses are stored in `DATA_DIR/integrations.json` and never shown again in the UI.

## Automatic light

Settings → **Printers** → pick a Centauri Carbon → **Options** → *Automatic light*. The light switches on when a print starts and off when it ends (after the end-of-print picture has been taken).

## External API

Other programs can read the print history, pictures and statistics and change reference numbers through a small HTTP API with API keys. It is off until you switch it on under Settings → **API access**. See [docs/external-api.md](docs/external-api.md).

## Reporting problems

The **Report bug** button in the footer (next to the version number) shows a diagnostics report — Spooler version, printer types and firmware, feature states and recent log lines — with IP addresses, serial numbers, printer names, MAC addresses and access codes removed. You can read it, copy or download it, and open a prefilled GitHub issue. Nothing is sent automatically. Pick the printer, describe the problem and it opens a prefilled GitHub issue. Can be switched off under Settings → Features (`report_problem`).

## Spoolman

Spooler integrates with **[Spoolman](https://github.com/Donkie/Spoolman)**, an open-source filament spool manager.

- Runs on **port 7912** alongside Spooler (started automatically by Docker Compose)
- Open **Spoolman UI** directly from the Spools panel
- Add spools manually
- **Filament catalogue import** (Settings → Integrations) imports any brand's filament types from SpoolmanDB (Elegoo preselected)
- Spool **location** in Spoolman is set automatically to the printer's name (e.g. `CC2`) whenever a spool is linked to a slot or the printer connects — no manual setup needed

By default, Spooler proxies the Spoolman UI through itself (so one port covers everything). To redirect the browser directly to Spoolman instead — useful if you run a separate reverse proxy or need better WebSocket support — set `PROXY_SPOOLMAN=false` in your `.env` file.

### Configuring the connection

Settings → **Integrations** lets you change the Spoolman URL, the proxy toggle, and an optional basic-auth username/password (for a Spoolman sitting behind a reverse proxy that requires it) — all without touching `.env` or restarting. **Test connection** checks it immediately. Values set via `.env` still work as the default if nothing's been changed in the UI; set `SPOOLER_LOCK_CONFIG=1` to make environment variables win unconditionally and the fields read-only (for someone hosting Spooler for other people).

The same page also has a **Slicer** field — paste the URL of a browser-based slicer (e.g. a self-hosted [Orca Slicer](https://github.com/linuxserver/docker-orcaslicer)) and a **Slicer** link appears in the sidebar that opens it in a new tab.

**Note:** integration values (including the optional Spoolman password) are stored unencrypted in `DATA_DIR/integrations.json`. Make sure that directory isn't readable by anyone you don't trust.

---

## Protocol

| Printer | Transport | Notes |
|---------|-----------|-------|
| CC1 (Centauri Carbon 1) | SDCP v3.0 over WebSocket (port 3030) | |
| CC2 (Centauri Carbon 2) | MQTT – printer hosts its own broker (port 1883) | Requires MQTT password from printer settings |
| Prusa (PrusaLink) | HTTP REST polling (`/api/v1`) | Requires API key from PrusaLink settings; experimental |
| Klipper (Moonraker) | HTTP REST polling (`/printer/objects/query`, port 7125) | API key optional; experimental |

See [CC2_INTEGRATION.md](CC2_INTEGRATION.md) for full CC2 protocol notes.

## Ports

| Port | Purpose |
|------|---------|
| 8080 | HTTP – web UI |
| 8443 | HTTPS – web UI (self-signed cert, needed for PWA) |
| 8765 | WebSocket – browser ↔ backend (plain, HTTP) |
| 8766 | WebSocket – browser ↔ backend (WSS, HTTPS/PWA) |
| 7912 | Spoolman – filament manager UI and API |
| 3030 | WebSocket – backend ↔ printer (CC1/SDCP) |
| 80 | HTTP – file upload to the printer (CC1, and CC2 `PUT /upload`) |
| 3000 | UDP – printer discovery broadcast (CC1) |
| 1883 | MQTT – CC2 printer broker (on the printer, not the server) |

## Development

Run the test suite locally:

```
pip install -r requirements-dev.txt
pytest
```

Tests run offline against fixtures — no real printer or Spoolman instance needed. CI runs the same suite on every pull request and on pushes to `dev`/`main`; the Docker image is only built and published once tests pass.

`GET /api/health` reports version, uptime, and per-printer connection status (name/type/connected, no IPs or access codes). No login required — it's what the Docker `HEALTHCHECK` and any external uptime monitoring hit.

## Tested firmware

Spooler talks to printers with reverse-engineered protocols, which can break when a manufacturer changes its firmware. The firmware versions known to work are listed in [`printers/tested_firmware.json`](printers/tested_firmware.json) (exact versions, or patterns with `*`, per printer type). A printer running anything else shows a small yellow **untested firmware** notice on its card — information only, nothing is blocked — and Spooler logs when a printer's firmware changes (you can also get a notification: Settings → Notifications → *Firmware changed*).

If you run a firmware version that works, add it to `tested_firmware.json` in the same pull request as any fix it needed (and say in the PR how you tested). If it doesn't work, please open an issue with the version.

## Stack

- **Backend** – Python 3.12, `asyncio`, `websockets`, `aiomqtt`, `bcrypt`
- **Frontend** – Vanilla HTML/CSS/JS, no build step
- **Spool manager** – [Spoolman](https://github.com/Donkie/Spoolman) (official Docker image)
- **Filament database** – [SpoolmanDB](https://github.com/Donkie/SpoolmanDB)

## Contributors

- [snazy2000](https://github.com/snazy2000) — modular backend refactor (auth, discovery, spoolman, printers modules), AMS/Canvas multi-material hub support

---

## Acknowledgements

CC2 support would not have been possible without these projects:

- [CentauriCarbon2](https://github.com/elegooofficial/CentauriCarbon2) by Elegoo (official, GPL-3.0) — firmware source; full MQTT method table (`method.h`), print state strings (`print_stats.cpp`), Canvas/AMS RFID filament struct (`canvas_dev.h`), sub_status codes, `gcode_move` speed/extrude factor fields
- [centauri-sentinel](https://github.com/LegalMarc/centauri-sentinel) by LegalMarc (MIT) — MQTT client details, topic structure, partial-status deep-merge, MJPEG grabber
- [elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant) by danielcherubini (MIT) — CC2 MQTT transport, access-code config, sub_status constants and method 1046 file metadata
- [elegoo-link](https://github.com/ELEGOO-3D/elegoo-link) by ELEGOO-3D (Apache-2.0) — CC2 MQTT method reference (method 1045 thumbnail, method 1046 file detail)
- [CentauriCarbon](https://github.com/elegooofficial/CentauriCarbon) by Elegoo (official, GPL-3.0) — CC1 firmware source; the error codes the printer shows on its screen (the English wording of those messages in `printers/error_codes.py` is taken from the firmware's `translation.csv`)
- [sdcp-centauri-carbon](https://github.com/WalkerFrederick/sdcp-centauri-carbon) by WalkerFrederick — SDCP v3.0 protocol documentation (CC1)
- [pycentauri](https://github.com/tholterhus/pycentauri) by tholterhus — notes on Centauri Carbon firmware behaviour (connection slots, quirks)
- centauri-carbon-dashboard (open source) — CC1 print speed control reference (SDCP Cmd 403 / PrintSpeedPct)
- [Spoolman](https://github.com/Donkie/Spoolman) by Donkie — open-source filament spool manager
- [SpoolmanDB](https://github.com/Donkie/SpoolmanDB) by Donkie — filament database for catalogue import
