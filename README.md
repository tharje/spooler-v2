# Spooler v2

Local web GUI for **Elegoo Centauri Carbon** FDM 3D printers. Control and monitor multiple printers from a single browser tab — or install as a PWA on your phone.

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

## Protocol

Uses **SDCP v3.0** (Smart Device Control Protocol) over WebSocket on port 3030. Status codes match the CarbonicSidecar / elegoo-homeassistant reference implementations.

## Ports

| Port | Purpose |
|------|---------|
| 8080 | HTTP – web UI |
| 8443 | HTTPS – web UI (self-signed cert, needed for PWA) |
| 8765 | WebSocket – browser ↔ backend |
| 7912 | Spoolman – filament manager UI and API |
| 3030 | WebSocket – backend ↔ printer (SDCP) |
| 3000 | UDP – printer discovery broadcast |

## Stack

- **Backend** – Python 3.12, `asyncio`, `websockets`
- **Frontend** – Vanilla HTML/CSS/JS, no build step
- **Spool manager** – [Spoolman](https://github.com/Donkie/Spoolman) (official Docker image)
- **Filament database** – [SpoolmanDB](https://github.com/Donkie/SpoolmanDB) (EAN lookup + catalogue import)
