# Spooler v2

Local web GUI for **Elegoo Centauri Carbon** FDM 3D printers. Control and monitor multiple printers from a single browser tab on your local network.

## Features

- **Live status** – accurate status mapping from SDCP v3.0 (idle, printing, warming up, paused, leveling, complete, cancelled)
- **Camera feed** – MJPEG stream direct from each printer
- **Temperature monitoring** – nozzle, bed and chamber with live values
- **Print progress** – layer count, percentage, elapsed and remaining time
- **Filament tracking** – live mm/g per print and cumulative history log
- **Controls** – pause, resume, stop, light on/off
- **Auto-discovery** – UDP broadcast finds printers on the local network
- **Persistence** – printers and print history saved between restarts

## Requirements

- Python 3.12+
- `python3-venv`

## Setup

```bash
git clone https://github.com/tharje/spooler-v2.git
cd spooler-v2
./setup.sh
```

Then open **http://localhost:8080** in your browser.

To access from other devices on your network use the machine's IP, e.g. `http://192.168.1.x:8080`.

## Adding printers

- Click **Discover** to auto-detect printers on the network (UDP port 3000)
- Click **Add Printer** to enter an IP address manually

Printer configs are saved in `printers.json` and reconnected automatically on next start.

## Filament history

All completed and cancelled prints are logged to `history.json` with:
- Filename
- Filament used (mm and grams, calculated for 1.75 mm PLA at 1.24 g/cm³)
- Print time
- Completion status

Open the **History** panel from the top bar to view totals and per-print breakdown.

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
