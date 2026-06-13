# Spooler v2

Local web GUI for **Elegoo Centauri Carbon** FDM 3D printers (CC1 and CC2). Control and monitor multiple printers from a single browser tab — or install as a PWA on your phone.

## Features

- **Live status** – accurate status mapping from SDCP v3.0 (idle, printing, warming up, paused, leveling, complete, cancelled)
- **Camera feed** – MJPEG stream direct from each printer
- **Temperature monitoring** – nozzle, bed and chamber with live values
- **Print progress** – layer count, percentage, elapsed and remaining time
- **Filament tracking** – live mm/g per print and cumulative history log
- **Controls** – pause, resume, stop, light toggle
- **Auto-discovery** – UDP broadcast finds printers on the local network
- **Persistence** – printers and print history saved between restarts
- **Spoolman integration** – filament spool inventory via [Spoolman](https://github.com/Donkie/Spoolman); auto-deducts used grams after each print, assign spools to printers
- **EAN barcode lookup** – scan a spool's barcode to auto-fill brand, material, colour and weight from SpoolmanDB
- **Filament catalogue import** – one-click import of all ELEGOO filaments from SpoolmanDB (97 entries with extruder temp, bed temp, spool weight and more)
- **Authentication** – password-protected login with session cookies; first-time setup via the web UI
- **PWA** – installable as a home screen app on Android and iOS (no app store needed)
- **HTTPS** – self-signed cert on port 8443 for secure PWA install; Tailscale serve works out of the box
- **Docker** – single `docker compose up -d` starts both Spooler and Spoolman

## Requirements

- Docker (recommended), or Python 3.12+

## Setup

### Option A – Docker (recommended)

```bash
git clone https://github.com/tharje/spooler-v2.git
cd spooler-v2
docker compose up -d
```

Open **http://\<server-ip\>:8080** in your browser.

> `network_mode: host` is required on Linux so Spooler can reach printers via UDP broadcast and WebSocket. On macOS/Windows use the Python setup below.

### Option B – Python (no Docker)

```bash
git clone https://github.com/tharje/spooler-v2.git
cd spooler-v2
./setup.sh
. venv/bin/activate && python3 server.py
```

Spoolman still needs to run separately (see [Spoolman docs](https://github.com/Donkie/Spoolman)).

## Install as PWA

On **Android** (Chrome): open `http://<server-ip>:8080` → tap ⋮ → **Add to Home Screen**  
On **iOS** (Safari): open the URL → tap Share → **Add to Home Screen**

For a proper standalone app (no browser chrome), use **HTTPS**:
- `https://<server-ip>:8443` with the self-signed cert, or
- [Tailscale serve](https://tailscale.com/kb/1312/serve) for a valid cert automatically

## Docker commands

```bash
docker compose up -d            # start
docker compose down             # stop (data preserved)
docker compose logs -f          # live logs
docker compose up -d --build    # rebuild after update
```

Data is stored in Docker volumes (`spooler_data`, `spoolman_data`) and survives restarts and rebuilds. Only `docker compose down -v` removes all data.

## Adding printers

- Click **Discover** to auto-detect printers on the network (UDP port 3000)
- Click **Add Printer** to enter an IP address manually

Printer configs are saved and reconnected automatically on next start.

## Filament history

All completed and cancelled prints are logged with filename, filament used (mm and grams), print time and completion status. Open the **History** panel from the sidebar.

## Spoolman

Spooler integrates with **[Spoolman](https://github.com/Donkie/Spoolman)** – the open-source filament spool manager.

- Spoolman runs on **port 7912** alongside Spooler (Docker Compose starts both)
- Open **Spoolman UI** directly from the Spools panel for advanced management
- Assign a spool to a printer from the printer card – used grams are deducted automatically when a print completes
- Add spools manually or scan the EAN barcode to auto-fill details from SpoolmanDB
- **Import Elegoo** button imports all 97 ELEGOO filament types from SpoolmanDB in one click, including material, colour, density, diameter, extruder temp, bed temp and spool weight
- Low/empty spool warnings appear on the printer card and as notifications

### Spool form fields

| Field | Default | Notes |
|-------|---------|-------|
| Brand | – | Loaded from SpoolmanDB (53 brands) |
| Material | – | Loaded from SpoolmanDB (51 types); auto-fills density |
| Color name | – | Free text |
| Color hex | `#888888` | Colour picker |
| Total weight (g) | 1000 | Net filament weight |
| Diameter (mm) | 1.75 | |
| Density (g/cm³) | 1.24 | Auto-filled when material is selected |

## Authentication

Spooler is password-protected by default. On first visit, the browser shows a **Create account** page where you choose a username and password. No terminal setup required.

The password hash is stored in `DATA_DIR/auth.json` inside the Docker volume and persists across restarts and rebuilds.

### Disable authentication (trusted LAN only)

Create a `.env` file in the project root:

```
AUTH_ENABLED=false
```

A warning is printed at startup when auth is off.

### Advanced: set credentials via environment variable

If you prefer to manage credentials outside the data volume (e.g. Docker secrets or CI), set `SPOOLER_PW_HASH` in `.env`. This takes priority over the saved file.

```bash
# Generate a bcrypt hash
docker compose exec spooler python3 server.py --hash-password
```

```env
SPOOLER_PW_HASH=$2b$12$...
SPOOLER_USERNAME=admin   # optional, default is whatever you set at first-time setup
```

See `.env.example` for all available options.

### How it works

| Part | Mechanism |
|------|-----------|
| Password | bcrypt hash saved to `DATA_DIR/auth.json` or `SPOOLER_PW_HASH` env var |
| Session | Random token (`secrets.token_urlsafe`), stored in memory, 30-day TTL |
| Cookie | `HttpOnly; SameSite=Strict`, `Secure` flag set automatically over HTTPS |
| HTTP API | 401 JSON on missing/invalid session; pages redirect to `/login` |
| WebSocket (port 8765) | Session cookie validated in the handshake — all printer commands protected |
| Camera feed | Proxied through `/api/camera/<id>` behind auth — not exposed directly from the printer |

## Protocol

| Printer | Transport | Notes |
|---------|-----------|-------|
| CC1 (Centauri Carbon 1) | SDCP v3.0 over WebSocket (port 3030) | Status codes match CarbonicSidecar / elegoo-homeassistant |
| CC2 (Centauri Carbon 2) | MQTT – printer hosts its own broker (port 1883) | Client subscribes to `elegoo/<serial>/api_status`; commands to `elegoo/<serial>/<client_id>/api_request` |

See [CC2_INTEGRATION.md](CC2_INTEGRATION.md) for full CC2 protocol notes, topic map, payload shape, and integration guidance.

## Ports

| Port | Purpose |
|------|---------|
| 8080 | HTTP – web UI |
| 8443 | HTTPS – web UI (self-signed cert, needed for PWA) |
| 8765 | WebSocket – browser ↔ backend |
| 7912 | Spoolman – filament manager UI and API |
| 3030 | WebSocket – backend ↔ printer (SDCP) |
| 3000 | UDP – printer discovery broadcast |
| 1883 | MQTT – CC2 printer broker (on the printer, not the server) |

## Stack

- **Backend** – Python 3.12, `asyncio`, `websockets`
- **Frontend** – Vanilla HTML/CSS/JS, no build step
- **Spool manager** – [Spoolman](https://github.com/Donkie/Spoolman) (official Docker image)
- **Filament database** – [SpoolmanDB](https://github.com/Donkie/SpoolmanDB) (EAN lookup + catalogue import)

## Acknowledgements

CC2 (Elegoo Centauri Carbon 2) support would not have been possible without the following projects. Huge thanks to their authors:

- [CentauriCarbon2](https://github.com/elegooofficial/CentauriCarbon2) by Elegoo (official) — CC2 firmware source; used for method codes (`method.h`), authoritative print state strings (`print_stats.cpp`: `"printing"`, `"paused"`, `"complete"`, `"cancelled"`, `"error"`), and machine-status sub_status codes
- [centauri-sentinel](https://github.com/LegalMarc/centauri-sentinel) by LegalMarc — MQTT client details, topic structure, partial-status deep-merge, MJPEG grabber
- [elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant) by danielcherubini — CC2 MQTT transport type, access-code config, and sub_status constants
- [sdcp-centauri-carbon](https://github.com/WalkerFrederick/sdcp-centauri-carbon) by WalkerFrederick — SDCP v3.0 protocol documentation (CC1)
- [Spoolman](https://github.com/Donkie/Spoolman) by Donkie — open-source filament spool manager
- [SpoolmanDB](https://github.com/Donkie/SpoolmanDB) by Donkie — filament database used for EAN lookup and catalogue import
