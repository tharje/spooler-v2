# Releases

## v2.2.0 — 2026-06-17

### Backend (contributed by [snazy2000](https://github.com/snazy2000))
- Modular refactor: server.py split into auth, state, persistence, discovery, spoolman, ws_handler, http_handler and printers/ modules
- AMS/Canvas multi-material hub support — active tray change auto-assigns the linked Spoolman spool

### Networking
- WSS WebSocket server on port 8766 — PWA and HTTPS users connect securely without mixed-content errors
- Fixed auth-status API endpoint crashing on unauthenticated requests

### Notifications
- Web Push / VAPID push notifications delivered to phone even when app is closed
- Notification types: print complete, layer checkpoint, nozzle still hot, nozzle overheat
- Settings → Notifications subpage with toggles and threshold inputs; Send test button

### UI
- Settings modal now has subpages: Change Password and Notifications
- Discover and Add Printer consolidated in header; + button removed from sidenav
- Camera stream excluded from service worker fetch interception (fixes MJPEG on iOS PWA)

---

## v2.1.1 — 2026-06-16

### UI
- Replaced gear icon on printer cards with pencil/edit icon
- Added Settings gear to sidenav with change-password modal
- Moved Sign out to bottom of sidenav, removed from header

### Bug fixes
- Fixed CC2 status showing "printing" after print completes (poll method 1002 every 15 s for fresh state; removed stale print_duration fallback)

---

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
