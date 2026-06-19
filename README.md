# Spooler v2

Local web GUI for **Elegoo Centauri Carbon** FDM 3D printers (CC1 and CC2). Monitor and control multiple printers from a single browser tab — or install as a PWA on your phone.

![Status: printing, paused, idle, complete](https://img.shields.io/badge/CC1%20%26%20CC2-supported-brightgreen)

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

- **Live status** – idle, printing, warming up, paused, leveling, complete, cancelled, error
- **Camera feed** – MJPEG stream from each printer (CC2), proxied through Spooler behind auth
- **Temperature monitoring** – nozzle, bed and chamber with live target values
- **Print progress** – layer count, percentage, elapsed and remaining time
- **Filament tracking** – live mm/g per print and cumulative history log
- **Controls** – pause, resume, stop, light toggle
- **Auto-discovery** – finds printers on the local network automatically
- **Persistence** – printers, history and spool data saved between restarts
- **Authentication** – password-protected login; first-time setup via the web UI, no terminal needed
- **Spoolman integration** – filament spool inventory with auto-deduction after each print
- **EAN barcode lookup** – scan a spool to auto-fill brand, material, colour and weight
- **Filament catalogue import** – one-click import of all ELEGOO filaments from SpoolmanDB
- **PWA** – installable as a home screen app on Android and iOS
- **HTTPS** – self-signed cert on port 8443; works with Tailscale serve out of the box

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

Printer configs are saved and reconnect automatically on restart.

---

## Filament tracking

- Live filament usage (mm and grams) shown on each printer card while printing
- All completed and cancelled prints are logged in the **History** panel
- Assign a Spoolman spool to a printer — used grams are deducted automatically after each print

---

## Spoolman

Spooler integrates with **[Spoolman](https://github.com/Donkie/Spoolman)**, an open-source filament spool manager.

- Runs on **port 7912** alongside Spooler (started automatically by Docker Compose)
- Open **Spoolman UI** directly from the Spools panel
- Add spools manually or scan an EAN barcode to auto-fill details
- **Import Elegoo** imports all 97 ELEGOO filament types from SpoolmanDB in one click

---

## Protocol

| Printer | Transport | Notes |
|---------|-----------|-------|
| CC1 (Centauri Carbon 1) | SDCP v3.0 over WebSocket (port 3030) | |
| CC2 (Centauri Carbon 2) | MQTT – printer hosts its own broker (port 1883) | Requires MQTT password from printer settings |

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
| 3000 | UDP – printer discovery broadcast |
| 1883 | MQTT – CC2 printer broker (on the printer, not the server) |

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

- [CentauriCarbon2](https://github.com/elegooofficial/CentauriCarbon2) by Elegoo (official) — firmware source; full MQTT method table (`method.h`), print state strings (`print_stats.cpp`), Canvas/AMS RFID filament struct (`canvas_dev.h`), sub_status codes, `gcode_move` speed/extrude factor fields
- [centauri-sentinel](https://github.com/LegalMarc/centauri-sentinel) by LegalMarc — MQTT client details, topic structure, partial-status deep-merge, MJPEG grabber
- [elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant) by danielcherubini — CC2 MQTT transport, access-code config and sub_status constants
- [sdcp-centauri-carbon](https://github.com/WalkerFrederick/sdcp-centauri-carbon) by WalkerFrederick — SDCP v3.0 protocol documentation (CC1)
- [Spoolman](https://github.com/Donkie/Spoolman) by Donkie — open-source filament spool manager
- [SpoolmanDB](https://github.com/Donkie/SpoolmanDB) by Donkie — filament database for EAN lookup and catalogue import
