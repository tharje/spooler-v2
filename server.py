#!/usr/bin/env python3
"""
Spooler – Elegoo Centauri Carbon GUI backend
WebSocket proxy + HTTP server for the web UI
"""

import asyncio
import json
import socket
import struct
import uuid
import time
import threading
import urllib.request
import urllib.parse
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

try:
    import websockets
    try:
        from websockets.asyncio.server import serve as ws_serve
        from websockets.asyncio.client import connect as ws_connect
    except ImportError:
        from websockets.server import serve as ws_serve
        from websockets.client import connect as ws_connect
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[WARN] 'websockets' package not found. Install it with:")
    print("       pip install websockets")
    print("       or run: ./setup.sh")

PRINTER_PORT = 3030
DISCOVERY_PORT = 3000
WS_SERVER_PORT = 8765
HTTP_PORT = 8080

PRINTERS_FILE = Path(__file__).parent / "printers.json"
HISTORY_FILE  = Path(__file__).parent / "history.json"

# Filament density g/cm³ for 1.75 mm diameter filament (default PLA)
FILAMENT_DENSITY = 1.24
FILAMENT_RADIUS_CM = 0.175 / 2  # 1.75mm → cm

def filament_mm_to_grams(mm):
    import math
    vol_cm3 = math.pi * FILAMENT_RADIUS_CM ** 2 * (mm / 10)
    return round(vol_cm3 * FILAMENT_DENSITY, 1)

# In-memory printer registry: { id: PrinterConnection }
printers = {}
# Connected browser clients
browser_clients = set()

# ─── Persistence ───────────────────────────────────────────────────────────────

def save_printers():
    data = [{"id": p.id, "ip": p.ip, "name": p.name} for p in printers.values()]
    PRINTERS_FILE.write_text(json.dumps(data, indent=2))

def load_printers():
    if not PRINTERS_FILE.exists():
        return []
    try:
        return json.loads(PRINTERS_FILE.read_text())
    except Exception:
        return []

def load_history():
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []

def append_history(entry):
    history = load_history()
    history.append(entry)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))

# ─── PrintInfo hex-field decoder ───────────────────────────────────────────────

def decode_printinfo(pi):
    """Elegoo firmware sends some field names as space-separated hex bytes.
    E.g. '54 6F 74 61 6C 45 78 74 72 75 73 69 6F 6E 00' → 'TotalExtrusion'"""
    result = {}
    for k, v in pi.items():
        parts = k.split()
        if (len(parts) > 1 and
                all(len(p) == 2 and all(c in '0123456789abcdefABCDEF' for c in p)
                    for p in parts)):
            try:
                decoded = bytes(int(p, 16) for p in parts).rstrip(b'\x00').decode('utf-8')
                result[decoded] = v
                continue
            except Exception:
                pass
        result[k] = v
    return result

# ─── UDP Discovery ─────────────────────────────────────────────────────────────

def discover_printers(timeout=3.0):
    """Send UDP broadcast M99999, collect responses."""
    found = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        sock.bind(("", 0))
        msg = b"M99999"
        sock.sendto(msg, ("<broadcast>", DISCOVERY_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                try:
                    info = json.loads(data.decode("utf-8"))
                    info["_ip"] = addr[0]
                    found.append(info)
                except Exception:
                    pass
            except socket.timeout:
                break
    except Exception as e:
        print(f"[Discovery] Error: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return found


# ─── SDCP Message Helpers ──────────────────────────────────────────────────────

def make_msg(cmd, data, mainboard_id=""):
    msg_id = str(uuid.uuid4()).replace("-", "")[:32]
    return {
        "Id": msg_id,
        "Data": {
            "Cmd": cmd,
            "Data": data,
            "RequestID": msg_id,
            "MainboardID": mainboard_id,
            "TimeStamp": int(time.time()),
            "From": "Web",
        },
        "Topic": f"sdcp/request/{mainboard_id}",
    }


CMD_STATUS      = 0
CMD_ATTRS       = 1
CMD_START       = 128
CMD_PAUSE       = 129
CMD_STOP        = 130
CMD_RESUME      = 131
CMD_LIST_FILES  = 258
CMD_DELETE_FILES = 259
CMD_CAMERA      = 386
CMD_LIGHT       = 403


# ─── Printer WebSocket Connection ─────────────────────────────────────────────

class PrinterConnection:
    def __init__(self, printer_id, ip, name, mainboard_id=""):
        self.id = printer_id
        self.ip = ip
        self.name = name
        self.mainboard_id = mainboard_id
        self.ws = None
        self.connected = False
        self.status = {}
        self.attrs = {}
        self.camera_url = None
        self._task = None
        self._last_print_status = None   # track status transitions
        self._print_start_time = None    # wall-clock when print started

    def to_dict(self):
        pi = decode_printinfo(self.status.get("PrintInfo", {}))
        filament_mm = pi.get("TotalExtrusion", 0) or 0
        return {
            "id": self.id,
            "ip": self.ip,
            "name": self.name,
            "mainboard_id": self.mainboard_id,
            "connected": self.connected,
            "status": self.status,
            "attrs": self.attrs,
            "camera_url": self.camera_url,
            "filament_mm": round(filament_mm, 1),
            "filament_g":  filament_mm_to_grams(filament_mm),
        }

    async def connect(self):
        url = f"ws://{self.ip}:{PRINTER_PORT}/websocket"
        try:
            print(f"[Printer {self.name}] Connecting to {url} …")
            self.ws = await ws_connect(url, ping_interval=20, ping_timeout=20)
            self.connected = True
            print(f"[Printer {self.name}] Connected!")
            await self._broadcast_state()
            # Request initial data
            await self.send_cmd(CMD_ATTRS, {})
            await self.send_cmd(CMD_STATUS, {})
            await self.send_cmd(CMD_CAMERA, {"Enable": True})
        except Exception as e:
            print(f"[Printer {self.name}] Connection failed: {e}")
            self.connected = False
            await self._broadcast_state()
            return

        try:
            async for raw in self.ws:
                await self._handle_message(raw)
        except Exception as e:
            print(f"[Printer {self.name}] Disconnected: {e}")
        finally:
            self.connected = False
            self.ws = None
            await self._broadcast_state()

    async def _handle_message(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Push: {"Status": { TempOfNozzle, PrintInfo, … }}
        if "Status" in msg and isinstance(msg["Status"], dict):
            self.status = msg["Status"]
            await self._check_print_transition()
            await self._broadcast_state()
            return

        # Push: {"Attributes": { Name, FirmwareVersion, … }}
        if "Attributes" in msg and isinstance(msg["Attributes"], dict):
            self.attrs = msg["Attributes"]
            mbid = self.attrs.get("MainboardID")
            if mbid and not self.mainboard_id:
                self.mainboard_id = mbid
            await self._broadcast_state()
            return

        # SDCP response: {"Data": {"Cmd": N, "Data": {…}}, "Topic": "sdcp/response/…"}
        data = msg.get("Data", {})
        cmd = data.get("Cmd")
        payload = data.get("Data", {})

        if cmd == CMD_ATTRS:
            if payload and payload != {"Ack": 0}:
                self.attrs = payload
                mbid = payload.get("MainboardID")
                if mbid and not self.mainboard_id:
                    self.mainboard_id = mbid
        elif cmd == CMD_STATUS:
            if payload and payload != {"Ack": 0}:
                self.status = payload
        elif cmd == CMD_CAMERA:
            url = payload.get("VideoUrl") or payload.get("Url")
            if url:
                self.camera_url = url if url.startswith("http") else f"http://{url}"
        elif cmd == CMD_LIGHT:
            print(f"[Printer {self.name}] Light response: {payload}")
            await self.send_cmd(CMD_STATUS, {})

        await self._broadcast_state()

    async def _check_print_transition(self):
        pi = decode_printinfo(self.status.get("PrintInfo", {}))
        cur_status = pi.get("Status")

        ACTIVE   = {1, 2, 3, 4, 7, 9, 10, 12, 13, 15, 16, 18, 19, 20, 21}
        PRINTING = {2, 3, 4, 13}

        # Print just started
        if cur_status in ACTIVE and self._last_print_status not in ACTIVE:
            self._print_start_time = time.time()

        # Print completed (status 9) or cancelled (8/14) after active printing
        if cur_status in (9, 8, 14, 0) and self._last_print_status in PRINTING | {5, 6}:
            filament_mm = pi.get("TotalExtrusion", 0) or 0
            filename    = pi.get("Filename", "")
            print_time  = pi.get("PrintTime", 0) or 0
            completed   = cur_status == 9
            if filament_mm > 0 or filename:
                entry = {
                    "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "printer_id":   self.id,
                    "printer_name": self.name,
                    "filename":     filename,
                    "filament_mm":  round(filament_mm, 1),
                    "filament_g":   filament_mm_to_grams(filament_mm),
                    "print_time_s": int(print_time),
                    "completed":    completed,
                }
                append_history(entry)
                label = "Completed" if completed else "Cancelled"
                print(f"[History] {label}: {filename} – {filament_mm:.0f}mm / {filament_mm_to_grams(filament_mm)}g")
                await broadcast_to_browsers({"type": "history_entry", "entry": entry})

        self._last_print_status = cur_status

    async def send_cmd(self, cmd, data):
        if not self.ws or not self.connected:
            return False
        msg = make_msg(cmd, data, self.mainboard_id)
        try:
            raw = json.dumps(msg)
            await self.ws.send(raw)
            return True
        except Exception as e:
            print(f"[Printer {self.name}] Send error: {e}")
            return False

    async def _broadcast_state(self):
        await broadcast_to_browsers({
            "type": "printer_update",
            "printer": self.to_dict(),
        })

    async def start(self):
        while True:
            await self.connect()
            if not self.connected:
                print(f"[Printer {self.name}] Retrying in 5 s …")
            await asyncio.sleep(5)


# ─── Browser WebSocket Server ──────────────────────────────────────────────────

async def broadcast_to_browsers(msg):
    if not browser_clients:
        return
    data = json.dumps(msg)
    dead = set()
    for client in list(browser_clients):
        try:
            await client.send(data)
        except Exception:
            dead.add(client)
    browser_clients.difference_update(dead)


async def browser_handler(websocket):
    browser_clients.add(websocket)
    print(f"[Browser] Client connected ({len(browser_clients)} total)")

    # Send current state immediately
    for p in printers.values():
        await websocket.send(json.dumps({
            "type": "printer_update",
            "printer": p.to_dict(),
        }))

    try:
        async for raw in websocket:
            await handle_browser_message(websocket, raw)
    except Exception:
        pass
    finally:
        browser_clients.discard(websocket)
        print(f"[Browser] Client disconnected ({len(browser_clients)} total)")


async def handle_browser_message(ws, raw):
    try:
        msg = json.loads(raw)
    except Exception:
        return

    action = msg.get("action")
    printer_id = msg.get("printer_id")
    printer = printers.get(printer_id)

    if action == "list_printers":
        for p in printers.values():
            await ws.send(json.dumps({"type": "printer_update", "printer": p.to_dict()}))
        return

    if action == "discover":
        await ws.send(json.dumps({"type": "info", "message": "Scanning network…"}))
        loop = asyncio.get_event_loop()
        found = await loop.run_in_executor(None, discover_printers)
        new_count = 0
        for dev in found:
            pid = dev.get("MainboardID") or dev.get("SerialNumber") or dev.get("_ip")
            if pid not in printers:
                name = dev.get("Name") or dev.get("MachineName") or f"Printer {len(printers)+1}"
                ip = dev.get("MainboardIP") or dev.get("_ip")
                pc = PrinterConnection(pid, ip, name, mainboard_id=pid)
                printers[pid] = pc
                asyncio.create_task(pc.start())
                new_count += 1
        if new_count:
            save_printers()
        await ws.send(json.dumps({"type": "info", "message": f"Found {len(found)} printer(s)"}))
        return

    if action == "add_printer":
        ip = msg.get("ip", "").strip()
        name = msg.get("name", f"Printer {len(printers)+1}").strip()
        if not ip:
            await ws.send(json.dumps({"type": "error", "message": "IP address required"}))
            return
        pid = ip  # use IP as ID when manually added
        if pid in printers:
            await ws.send(json.dumps({"type": "info", "message": "Printer already added"}))
            return
        pc = PrinterConnection(pid, ip, name)
        printers[pid] = pc
        asyncio.create_task(pc.start())
        save_printers()
        await ws.send(json.dumps({"type": "info", "message": f"Adding {name} ({ip})…"}))
        return

    if action == "remove_printer":
        p = printers.pop(printer_id, None)
        if p and p.ws:
            await p.ws.close()
        save_printers()
        await broadcast_to_browsers({"type": "printer_removed", "printer_id": printer_id})
        return

    if not printer:
        await ws.send(json.dumps({"type": "error", "message": "Printer not found"}))
        return

    cmd_map = {
        "pause":  (CMD_PAUSE,  {}),
        "resume": (CMD_RESUME, {}),
        "stop":   (CMD_STOP,   {}),
        "status": (CMD_STATUS, {}),
        "light_on":  (CMD_LIGHT, {"LightStatus": {"SecondLight": True,  "RgbLight": [0, 0, 0]}}),
        "light_off": (CMD_LIGHT, {"LightStatus": {"SecondLight": False, "RgbLight": [0, 0, 0]}}),
        "camera_on": (CMD_CAMERA, {"Enable": True}),
    }

    if action == "list_files":
        ok = await printer.send_cmd(CMD_LIST_FILES, {"Url": "/", "IsDir": True})
        if not ok:
            await ws.send(json.dumps({"type": "error", "message": "Printer not connected"}))
        return

    if action == "delete_files":
        files = msg.get("files", [])
        await printer.send_cmd(CMD_DELETE_FILES, {"FileList": files})
        return

    if action in cmd_map:
        cmd, data = cmd_map[action]
        ok = await printer.send_cmd(cmd, data)
        if not ok:
            await ws.send(json.dumps({"type": "error", "message": "Printer not connected"}))
        elif action in ("light_on", "light_off"):
            await asyncio.sleep(0.5)
            await printer.send_cmd(CMD_STATUS, {})


# ─── HTTP File Upload Proxy ────────────────────────────────────────────────────

class SPHandler(SimpleHTTPRequestHandler):
    """Serve static files from ./public and handle /api/ calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent / "public"), **kwargs)

    def log_message(self, fmt, *args):
        pass  # silence access log

    def do_GET(self):
        if self.path == "/api/printers":
            self._json([p.to_dict() for p in printers.values()])
        elif self.path == "/api/history":
            self._json(load_history())
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/upload/"):
            printer_id = urllib.parse.unquote(self.path[len("/api/upload/"):])
            printer = printers.get(printer_id)
            if not printer:
                self._json({"error": "Printer not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            # Forward to printer
            try:
                req = urllib.request.Request(
                    f"http://{printer.ip}/uploadFile/upload",
                    data=body,
                    headers={"Content-Type": self.headers.get("Content-Type", "application/octet-stream")},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = resp.read()
                self._json({"ok": True, "response": result.decode("utf-8", errors="replace")})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "Not found"}, 404)

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def run_http(port):
    server = HTTPServer(("0.0.0.0", port), SPHandler)
    print(f"[HTTP] Serving on http://0.0.0.0:{port}")
    server.serve_forever()


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not WEBSOCKETS_AVAILABLE:
        print("\n[ERROR] Please install the 'websockets' package first.")
        print("  python3 -m venv venv && . venv/bin/activate && pip install websockets")
        sys.exit(1)

    # Load saved printers
    for entry in load_printers():
        pid, ip, name = entry["id"], entry["ip"], entry["name"]
        pc = PrinterConnection(pid, ip, name)
        printers[pid] = pc
        asyncio.create_task(pc.start())
    if printers:
        print(f"[Config] Loaded {len(printers)} saved printer(s)")

    # Start HTTP server in background thread
    http_thread = threading.Thread(target=run_http, args=(HTTP_PORT,), daemon=True)
    http_thread.start()

    # Start browser WS server
    print(f"[WS]   Browser WebSocket on ws://0.0.0.0:{WS_SERVER_PORT}")
    async with ws_serve(browser_handler, "0.0.0.0", WS_SERVER_PORT):
        print("\n" + "─" * 50)
        print(f"  Spooler is running!")
        print(f"  Open: http://localhost:{HTTP_PORT}")
        print("─" * 50 + "\n")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
