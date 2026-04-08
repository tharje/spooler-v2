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
- **Reel integration** – filament spool inventory via [Reel](https://github.com/tharje/reel); auto-deducts used grams after each print
- **EAN barcode lookup** – scan a spool's barcode to auto-fill brand, material, colour and weight from SpoolmanDB
- **PWA** – installable as a home screen app on Android and iOS (no app store needed)
- **Docker** – single `docker compose up -d` starts both Spooler and Reel

## Requirements

- Python 3.12+  **or**  Docker

## Setup

### Option A – Docker (recommended)

```bash
git clone https://github.com/tharje/spooler-v2.git Spooler
git clone https://github.com/tharje/reel.git Reel

cd Spooler
docker compose up -d
```

Open **http://localhost:8080** in your browser.

> `network_mode: host` is used so Spooler can reach printers via UDP broadcast and WebSocket. This requires Linux. On macOS/Windows use the Python setup below.

### Option B – Python

```bash
git clone https://github.com/tharje/spooler-v2.git
cd spooler-v2
./setup.sh
. venv/bin/activate && python3 server.py
```

Open **http://localhost:8080** in your browser.

## Install as app (PWA)

On **Android** (Chrome): open `http://<server-ip>:8080` → tap ⋮ → **Add to Home Screen**  
On **iOS** (Safari): open the URL → tap Share → **Add to Home Screen**

Spooler appears as a full-screen app with its own icon — no app store required.

## Docker commands

```bash
docker compose up -d                        # start
docker compose down                         # stop (data is preserved)
docker compose logs -f                      # live logs
docker compose up -d --build               # rebuild after update
```

Data is stored in Docker volumes (`spooler_data`, `reel_data`) and survives restarts and rebuilds. Only `docker compose down -v` removes data.

## Adding printers

- Click **Discover** to auto-detect printers on the network (UDP port 3000)
- Click **Add Printer** to enter an IP address manually

Printer configs are saved in `printers.json` and reconnected automatically on next start.

## Filament history

All completed and cancelled prints are logged with filename, filament used (mm and grams), print time, and completion status. Open the **History** panel from the sidebar.

Filament weight is calculated for 1.75 mm filament at 1.24 g/cm³ (PLA default).

## Reel

Spooler integrates with **[Reel](https://github.com/tharje/reel)** – a companion service for managing your filament spool inventory.

- Assign a spool to a printer from the printer card
- Used grams are deducted automatically when a print completes
- Add spools by scanning the EAN barcode on the box — brand, material, colour and weight are filled in automatically from SpoolmanDB

## Protocol

Uses **SDCP v3.0** (Smart Device Control Protocol) over WebSocket on port 3030. Status codes match the CarbonicSidecar / elegoo-homeassistant reference implementations.

## Ports

| Port | Purpose |
|------|---------|
| 8080 | HTTP – serves the web UI |
| 8765 | WebSocket – browser ↔ backend |
| 3030 | WebSocket – backend ↔ printer (SDCP) |
| 3000 | UDP – printer discovery |

## Stack

- **Backend** – Python 3.12, `asyncio`, `websockets`
- **Frontend** – Vanilla HTML/CSS/JS, no build step
- **Spool database** – [Reel](https://github.com/tharje/reel) (FastAPI + SQLite)
