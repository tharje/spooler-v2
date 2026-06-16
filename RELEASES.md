# Releases

## v2.1.0 — 2026-06-16

### Bug fixes
- Fixed CC2 light button not reflecting state after click
- Fixed CC2 print progress showing over 100% (layer count was used as percentage instead of layer ratio)
- Fixed CC2 filament usage stuck at the value from initial connection (stale value from api_response method 1002 was cached and blocked live tracking)
- Fixed CC1 camera feed dropping during printing (read timeout on slow MJPEG stream)

### New features
- **Authentication** — password-protected login with first-time setup via web UI; no terminal or config file needed
- **Spoolman integration** — filament spool inventory linked to each printer; used grams deducted automatically after each print
- **ELEGOO filament catalogue** — one-click import of all 97 ELEGOO filament types from SpoolmanDB
- **EAN barcode scan** — scan a spool's barcode to auto-fill brand, material, colour and weight
- **Docker image on ghcr.io** — two-command install (`curl` + `docker compose up -d`), no git clone needed

---

## v2.0.0 — 2026-05-20

### Initial release
- CC1 (Centauri Carbon 1) support via SDCP v3.0 over WebSocket
- CC2 (Centauri Carbon 2) support via MQTT
- Live status: idle, printing, warming up, paused, leveling, complete, cancelled, error
- Temperature monitoring: nozzle, bed and chamber with live target values
- Print progress: layer count, percentage, elapsed and remaining time
- Filament tracking: live mm/g per print and cumulative history log
- Camera feed proxied through Spooler (CC2)
- Controls: pause, resume, stop, light toggle
- Auto-discovery via UDP broadcast
- PWA: installable on Android and iOS
- HTTPS with self-signed cert on port 8443
