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
import ssl
import subprocess
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

try:
    import aiomqtt
    AIOMQTT_AVAILABLE = True
except ImportError:
    AIOMQTT_AVAILABLE = False

PRINTER_PORT = 3030
DISCOVERY_PORT = 3000
WS_SERVER_PORT = 8765
HTTP_PORT  = 8080
HTTPS_PORT = 8443

DATA_DIR         = Path(os.getenv("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PRINTERS_FILE    = DATA_DIR / "printers.json"
HISTORY_FILE     = DATA_DIR / "history.json"
SPOOLMAN_URL     = "http://localhost:7912"
CERT_FILE        = DATA_DIR / "cert.pem"
KEY_FILE         = DATA_DIR / "key.pem"

def ensure_ssl_cert():
    """Generate a self-signed cert if one doesn't exist yet."""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return True
    try:
        # Get local IP for the SAN so the cert covers the real address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"
    try:
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
            "-days", "3650", "-nodes",
            "-subj", f"/CN=spooler.local",
            "-addext", f"subjectAltName=IP:{local_ip},IP:127.0.0.1,DNS:localhost",
        ], check=True, capture_output=True)
        print(f"[SSL] Certificate generated (IP: {local_ip})")
        return True
    except Exception as e:
        print(f"[SSL] Could not generate certificate: {e}")
        return False
SPOOLMAN_DB_URL  = "https://donkie.github.io/SpoolmanDB/filaments.json"
SPOOLMAN_DB_TTL  = 3600  # re-fetch at most once per hour

_spoolman_db          = None
_spoolman_db_fetched  = 0.0

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
    data = [{"id": p.id, "ip": p.ip, "name": p.name,
              "printer_type": p.printer_type, "access_code": p.access_code}
            for p in printers.values()]
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

def get_spoolman_db():
    global _spoolman_db, _spoolman_db_fetched
    if _spoolman_db is not None and time.time() - _spoolman_db_fetched < SPOOLMAN_DB_TTL:
        return _spoolman_db
    try:
        with urllib.request.urlopen(SPOOLMAN_DB_URL, timeout=10) as resp:
            _spoolman_db = json.loads(resp.read())
            _spoolman_db_fetched = time.time()
            print(f"[SpoolmanDB] Loaded {len(_spoolman_db)} filaments")
    except Exception as e:
        print(f"[SpoolmanDB] Fetch failed: {e}")
        if _spoolman_db is None:
            _spoolman_db = []
    return _spoolman_db

def spoolman_deduct(printer_id: str, amount_g: float):
    """Deduct used filament from the spool assigned to this printer in Spoolman."""
    try:
        url = f"{SPOOLMAN_URL}/api/v1/spool?location={urllib.parse.quote(printer_id)}"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
        if not data:
            return
        spool = data[0]
        spool_id = spool["id"]
        body = json.dumps({"use_weight": round(amount_g, 1)}).encode()
        req = urllib.request.Request(
            f"{SPOOLMAN_URL}/api/v1/spool/{spool_id}/use",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read())
        remaining = result.get("remaining_weight", 0)
        total     = result.get("initial_weight", 0)
        name      = result.get("filament", {}).get("name") or f"Spool {spool_id}"
        print(f"[Spoolman] {amount_g}g deducted from '{name}' → {remaining}g left")

        # Notify browser if spool is empty or critically low (< 10 %)
        loop = asyncio.get_event_loop()
        if remaining == 0:
            msg = {"type": "spool_empty",
                   "spool": result, "printer_id": printer_id}
        elif total > 0 and (remaining / total) < 0.1:
            msg = {"type": "spool_low",
                   "spool": result, "printer_id": printer_id}
        else:
            msg = None

        if msg:
            asyncio.run_coroutine_threadsafe(broadcast_to_browsers(msg), loop)
    except Exception as e:
        print(f"[Spoolman] Deduct skipped ({e})")

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


def _deep_merge(base: dict, incoming: dict, max_keys: int = 500) -> dict:
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v, max_keys)
        else:
            base[k] = v
        if len(base) > max_keys:
            break
    return base


# ─── Printer WebSocket Connection ─────────────────────────────────────────────

class PrinterConnection:
    def __init__(self, printer_id, ip, name, mainboard_id="", printer_type="cc1", access_code=""):
        self.id = printer_id
        self.ip = ip
        self.name = name
        self.mainboard_id = mainboard_id
        self.printer_type = printer_type
        self.access_code = access_code
        self.ws = None
        self.connected = False
        self.status = {}
        self.attrs = {}
        self.camera_url = f"http://{ip}:8080/mjpeg" if printer_type == "cc2" else None
        self._task = None
        self._last_print_status = None
        self._print_start_time = None
        # CC2 MQTT state
        self._mqtt_client = None
        self._mqtt_serial = None
        self._mqtt_client_id = None
        self._cc2_state = {}

    def to_dict(self):
        pi = decode_printinfo(self.status.get("PrintInfo", {}))
        filament_mm = pi.get("TotalExtrusion", 0) or 0
        return {
            "id": self.id,
            "ip": self.ip,
            "name": self.name,
            "printer_type": self.printer_type,
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
                if filament_mm > 0:
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(None, spoolman_deduct, self.id, filament_mm_to_grams(filament_mm))

        self._last_print_status = cur_status

    async def send_cmd(self, cmd, data):
        if self.printer_type == "cc2":
            return await self._send_mqtt_cmd(cmd)
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

    async def _send_mqtt_cmd(self, cmd):
        CC2_METHODS = {CMD_PAUSE: 1001, CMD_RESUME: 1002, CMD_STOP: 1003}
        method = CC2_METHODS.get(cmd)
        if not method or not self._mqtt_client or not self._mqtt_serial:
            return False
        topic = f"elegoo/{self._mqtt_serial}/{self._mqtt_client_id}/api_request"
        payload = json.dumps({"id": uuid.uuid4().int & 0xFFFF, "method": method})
        try:
            await self._mqtt_client.publish(topic, payload)
            return True
        except Exception as e:
            print(f"[Printer {self.name}] MQTT send error: {e}")
            return False

    async def connect_mqtt(self):
        if not AIOMQTT_AVAILABLE:
            print(f"[Printer {self.name}] aiomqtt not installed — CC2 unavailable")
            await self._broadcast_state()
            return
        try:
            print(f"[Printer {self.name}] Connecting via MQTT to {self.ip}:1883 …")
            async with aiomqtt.Client(
                hostname=self.ip,
                port=1883,
                username="elegoo",
                password=self.access_code,
            ) as client:
                self._mqtt_client = client
                self._mqtt_client_id = str(uuid.uuid4()).replace("-", "")
                self.connected = True
                self.camera_url = f"http://{self.ip}:8080/mjpeg"
                print(f"[Printer {self.name}] MQTT connected!")
                await self._broadcast_state()
                await client.subscribe("elegoo/+/api_status")
                async for message in client.messages:
                    await self._handle_mqtt_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Printer {self.name}] MQTT failed: {e}")
        finally:
            self._mqtt_client = None
            self.connected = False
            self.camera_url = f"http://{self.ip}:8080/mjpeg"  # keep camera visible while offline
            await self._broadcast_state()

    async def _handle_mqtt_message(self, message):
        try:
            payload = json.loads(message.payload.decode())
        except Exception:
            return
        if not self._mqtt_serial:
            parts = str(message.topic).split("/")
            if len(parts) >= 2:
                self._mqtt_serial = parts[1]
        result = payload.get("result", {})
        if not isinstance(result, dict) or not result:
            return
        _deep_merge(self._cc2_state, result)
        self._apply_cc2_status()
        await self._check_print_transition()
        await self._broadcast_state()

    def _apply_cc2_status(self):
        s   = self._cc2_state
        ps  = s.get("print_status", {})
        gm  = s.get("gcode_move", {})
        ext = s.get("extruder", {})
        bed = s.get("heater_bed", {})

        print_duration = ps.get("print_duration", 0) or 0
        remaining      = ps.get("remaining_time_sec", 0) or 0

        # CC2 has no state field; infer from durations
        if print_duration > 0 or remaining > 0:
            status_code = 3   # printing
        else:
            status_code = 0   # idle

        total    = print_duration + remaining
        progress = round(print_duration / total * 100) if total > 0 else 0

        self.status = {
            "PrintInfo": {
                "Status":         status_code,
                "CurrentLayer":   ps.get("current_layer", 0),
                "CurrentTicks":   progress,
                "TotalTicks":     100,
                "PrintTime":      print_duration,
                "RemainTime":     remaining,
                "TotalExtrusion": gm.get("extruder", 0),
            },
            "TempOfNozzle":     ext.get("temperature", 0),
            "TempTargetNozzle": ext.get("target", 0),
            "TempOfHotbed":     bed.get("temperature", 0),
            "TempTargetHotbed": bed.get("target", 0),
        }

    async def _broadcast_state(self):
        await broadcast_to_browsers({
            "type": "printer_update",
            "printer": self.to_dict(),
        })

    def stop(self):
        if self._task:
            self._task.cancel()

    async def start(self):
        try:
            while True:
                if self.printer_type == "cc2":
                    await self.connect_mqtt()
                else:
                    await self.connect()
                if not self.connected:
                    print(f"[Printer {self.name}] Retrying in 5 s …")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass


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
                pc._task = asyncio.create_task(pc.start())
                new_count += 1
        if new_count:
            save_printers()
        await ws.send(json.dumps({"type": "info", "message": f"Found {len(found)} printer(s)"}))
        return

    if action == "add_printer":
        ip           = msg.get("ip", "").strip()
        name         = msg.get("name", f"Printer {len(printers)+1}").strip()
        printer_type = msg.get("printer_type", "cc1")
        access_code  = msg.get("access_code", "").strip()
        if not ip:
            await ws.send(json.dumps({"type": "error", "message": "IP address required"}))
            return
        pid = ip
        if pid in printers:
            await ws.send(json.dumps({"type": "info", "message": "Printer already added"}))
            return
        pc = PrinterConnection(pid, ip, name, printer_type=printer_type, access_code=access_code)
        printers[pid] = pc
        pc._task = asyncio.create_task(pc.start())
        save_printers()
        await ws.send(json.dumps({"type": "info", "message": f"Adding {name} ({ip})…"}))
        return

    if action == "remove_printer":
        p = printers.pop(printer_id, None)
        if p:
            p.stop()
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
        if self.path == "/cert.pem":
            # Let users download and install the self-signed cert on their device
            data = CERT_FILE.read_bytes() if CERT_FILE.exists() else b""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-pem-file")
            self.send_header("Content-Disposition", 'attachment; filename="spooler-cert.pem"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/api/printers":
            self._json([p.to_dict() for p in printers.values()])
        elif self.path == "/api/history":
            self._json(load_history())
        elif self.path.startswith("/api/lookup-ean"):
            qs     = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            ean    = params.get("ean", [""])[0].strip()
            if not ean:
                self._json({"error": "ean required"}, 400)
                return
            db = get_spoolman_db()
            for item in db:
                codes = item.get("ean") or []
                if ean in codes:
                    self._json(item)
                    return
            self._json({"error": "Not found"}, 404)
        elif self.path == "/api/filament-meta":
            db = get_spoolman_db()
            brands = sorted({item.get("manufacturer", "") for item in db if item.get("manufacturer")})
            mat_map = {}
            for item in db:
                mat = item.get("material") or ""
                den = item.get("density")
                if mat and mat not in mat_map and den:
                    mat_map[mat] = den
            materials = [{"name": m, "density": mat_map[m]} for m in sorted(mat_map)]
            self._json({"brands": brands, "materials": materials})
        elif self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("GET", self.path[len("/api/spoolman"):], None)
        else:
            super().do_GET()

    def do_PATCH(self):
        if self.path.startswith("/api/spoolman"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None
            self._proxy_spoolman("PATCH", self.path[len("/api/spoolman"):], body)
        else:
            self._json({"error": "Not found"}, 404)

    def _proxy_spoolman(self, method, path, body):
        try:
            req = urllib.request.Request(
                f"{SPOOLMAN_URL}{path or '/'}",
                data=body,
                headers={"Content-Type": "application/json"} if body else {},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                status = resp.status
            if not data:
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
            else:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self._json({"error": str(e)}, e.code)
        except Exception as e:
            self._json({"error": f"Spoolman unreachable: {e}"}, 502)

    def do_DELETE(self):
        if self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("DELETE", self.path[len("/api/spoolman"):], None)
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/api/import-filaments":
            length = int(self.headers.get("Content-Length", 0))
            req_body = json.loads(self.rfile.read(length)) if length else {}
            brand = req_body.get("brand", "").strip()
            if not brand:
                self._json({"error": "brand required"}, 400)
                return
            db = get_spoolman_db()
            entries = [f for f in db if f.get("manufacturer", "").lower() == brand.lower()]
            if not entries:
                self._json({"error": f"No filaments found for '{brand}'"}, 404)
                return
            # Find or create vendor
            try:
                vurl = f"{SPOOLMAN_URL}/api/v1/vendor?name={urllib.parse.quote(brand)}"
                with urllib.request.urlopen(vurl, timeout=5) as r:
                    vendors = json.loads(r.read())
                if vendors:
                    vendor_id = vendors[0]["id"]
                else:
                    vreq = urllib.request.Request(
                        f"{SPOOLMAN_URL}/api/v1/vendor",
                        data=json.dumps({"name": brand}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(vreq, timeout=5) as r:
                        vendor_id = json.loads(r.read())["id"]
            except Exception as e:
                self._json({"error": f"Vendor error: {e}"}, 500)
                return
            # Fetch existing article numbers to avoid duplicates
            try:
                with urllib.request.urlopen(f"{SPOOLMAN_URL}/api/v1/filament?limit=10000", timeout=5) as r:
                    existing = {f["article_number"] for f in json.loads(r.read()) if f.get("article_number")}
            except Exception:
                existing = set()
            # Import filaments
            created = skipped = 0
            for f in entries:
                article = f.get("id", "")
                if article and article in existing:
                    skipped += 1
                    continue
                body = {
                    "vendor_id":            vendor_id,
                    "material":             f.get("material", ""),
                    "density":              f.get("density")  or 1.24,
                    "diameter":             f.get("diameter") or 1.75,
                    "weight":               f.get("weight")   or 1000,
                }
                if f.get("name"):          body["name"]                    = f["name"]
                if f.get("spool_weight"):  body["spool_weight"]             = f["spool_weight"]
                if f.get("color_hex"):     body["color_hex"]                = f["color_hex"].lstrip("#")
                if f.get("extruder_temp"): body["settings_extruder_temp"]   = f["extruder_temp"]
                if f.get("bed_temp"):      body["settings_bed_temp"]        = f["bed_temp"]
                if article:               body["article_number"]           = article
                try:
                    freq = urllib.request.Request(
                        f"{SPOOLMAN_URL}/api/v1/filament",
                        data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(freq, timeout=5) as r:
                        r.read()
                    created += 1
                except Exception:
                    skipped += 1
            print(f"[Import] {brand}: {created} created, {skipped} skipped")
            self._json({"brand": brand, "created": created, "skipped": skipped, "total": len(entries)})
            return
        if self.path.startswith("/api/spoolman"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None
            self._proxy_spoolman("POST", self.path[len("/api/spoolman"):], body)
        elif self.path.startswith("/api/upload/"):
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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

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

def run_https(port):
    if not ensure_ssl_cert():
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    server = HTTPServer(("0.0.0.0", port), SPHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"[HTTPS] Serving on https://0.0.0.0:{port}")
    server.serve_forever()


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not WEBSOCKETS_AVAILABLE:
        print("\n[ERROR] Please install the 'websockets' package first.")
        print("  python3 -m venv venv && . venv/bin/activate && pip install websockets")
        sys.exit(1)

    # Load saved printers
    for entry in load_printers():
        pid  = entry["id"]
        ip   = entry["ip"]
        name = entry["name"]
        ptype = entry.get("printer_type", "cc1")
        acode = entry.get("access_code", "")
        pc = PrinterConnection(pid, ip, name, printer_type=ptype, access_code=acode)
        printers[pid] = pc
        pc._task = asyncio.create_task(pc.start())
    if printers:
        print(f"[Config] Loaded {len(printers)} saved printer(s)")

    # Start HTTP + HTTPS servers in background threads
    http_thread = threading.Thread(target=run_http, args=(HTTP_PORT,), daemon=True)
    http_thread.start()
    https_thread = threading.Thread(target=run_https, args=(HTTPS_PORT,), daemon=True)
    https_thread.start()

    # Start browser WS server
    print(f"[WS]   Browser WebSocket on ws://0.0.0.0:{WS_SERVER_PORT}")
    async with ws_serve(browser_handler, "0.0.0.0", WS_SERVER_PORT):
        print("\n" + "─" * 50)
        print(f"  Spooler is running!")
        print(f"  HTTP:  http://localhost:{HTTP_PORT}")
        print(f"  HTTPS: https://localhost:{HTTPS_PORT}  ← use this for PWA")
        print("─" * 50 + "\n")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
