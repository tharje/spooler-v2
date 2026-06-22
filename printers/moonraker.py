"""
Klipper/Moonraker printer connection via Moonraker HTTP API.

Polls /printer/objects/query every 2 seconds — Moonraker supports WebSocket push
but HTTP polling is simpler and consistent with the Prusa implementation.
API key is optional (sent as X-Api-Key header if provided).
Default Moonraker port: 7125.
"""

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

import state
from printers.base import PrinterConnection
from printers.protocol import CMD_PAUSE, CMD_RESUME, CMD_STOP

POLL_INTERVAL = 2.0

# Moonraker /printer/objects/query fields we need
_QUERY_PATH = (
    "/printer/objects/query"
    "?print_stats&display_status&extruder&heater_bed"
)

_STATE_MAP = {
    "standby":   0,
    "printing":  3,
    "paused":    6,
    "complete":  9,
    "error":     14,
    "cancelled": 8,
}


class MoonrakerConnection(PrinterConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, printer_type="moonraker", **kwargs)
        self._base = f"http://{self.ip}:7125"
        self._expected_filament_mm = 0.0
        self._expected_print_time_s = 0
        self._last_filename = ""

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.access_code:
            req.add_header("X-Api-Key", self.access_code)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code in (204, 205):
                return {}
            raise

    async def _req(self, method: str, path: str, body: dict | None = None):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._request, method, path, body)

    # ── Connection loop ───────────────────────────────────────────────────────

    async def connect(self) -> None:
        print(f"[Printer {self.name}] Connecting to Moonraker at {self._base} …")
        fail_streak = 0
        while True:
            try:
                raw = await self._req("GET", _QUERY_PATH)
                fail_streak = 0
                if not self.connected:
                    self.connected = True
                    print(f"[Printer {self.name}] Connected!")
                    await self._broadcast_state()
                self._apply_status(raw.get("result", {}).get("status", {}))
                await self._check_print_transition()
                await self._broadcast_state()
            except Exception as e:
                fail_streak += 1
                if self.connected or fail_streak == 1:
                    print(f"[Printer {self.name}] Error: {e}")
                if self.connected:
                    self.connected = False
                    await self._broadcast_state()
                await asyncio.sleep(POLL_INTERVAL * 3)
                continue
            await asyncio.sleep(POLL_INTERVAL)

    # ── Status mapping ────────────────────────────────────────────────────────

    def _apply_status(self, status: dict) -> None:
        ps  = status.get("print_stats") or {}
        ds  = status.get("display_status") or {}
        ext = status.get("extruder") or {}
        bed = status.get("heater_bed") or {}

        raw_state   = ps.get("state") or "standby"
        status_code = _STATE_MAP.get(raw_state, 0)

        filename       = ps.get("filename") or ""
        print_duration = float(ps.get("print_duration") or 0)
        filament_mm    = float(ps.get("filament_used") or 0)
        progress       = float(ds.get("progress") or 0) * 100.0

        # Fetch file metadata when a new print starts
        if filename and filename != self._last_filename and status_code in (3, 6):
            self._last_filename = filename
            self._expected_filament_mm = 0.0
            self._expected_print_time_s = 0
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._fetch_file_metadata(filename))
            except RuntimeError:
                pass

        # Estimate remaining time from metadata + progress
        remaining = 0
        if self._expected_print_time_s > 0 and progress > 0:
            remaining = max(0, round(self._expected_print_time_s * (1.0 - progress / 100.0)))

        if status_code == 0:
            filament_mm    = 0.0
            remaining      = 0
            print_duration = 0.0

        self.status = {
            "PrintInfo": {
                "Status":         status_code,
                "CurrentLayer":   0,
                "TotalLayer":     0,
                "CurrentTicks":   round(progress),
                "TotalTicks":     100,
                "PrintTime":      round(print_duration),
                "RemainTime":     remaining,
                "TotalExtrusion": filament_mm,
                "Filename":       filename,
            },
            "TempOfNozzle":     float(ext.get("temperature") or 0),
            "TempTargetNozzle": float(ext.get("target") or 0),
            "TempOfHotbed":     float(bed.get("temperature") or 0),
            "TempTargetHotbed": float(bed.get("target") or 0),
            "SpeedFactor":      100,
        }

    async def _fetch_file_metadata(self, filename: str) -> None:
        try:
            path = f"/server/files/metadata?filename={urllib.parse.quote(filename)}"
            raw  = await self._req("GET", path)
            result = raw.get("result") or {}
            ft = result.get("filament_total")
            if ft:
                self._expected_filament_mm = float(ft)
            pt = result.get("estimated_time")
            if pt:
                self._expected_print_time_s = int(pt)
            if self._expected_filament_mm or self._expected_print_time_s:
                print(f"[Printer {self.name}] File metadata: "
                      f"{self._expected_filament_mm:.0f}mm / {self._expected_print_time_s}s")
        except Exception:
            pass

    # ── Commands ───────────────────────────────────────────────────────────────

    async def send_cmd(self, cmd, data=None) -> bool:
        try:
            if cmd == CMD_PAUSE:
                await self._req("POST", "/printer/print/pause")
            elif cmd == CMD_RESUME:
                await self._req("POST", "/printer/print/resume")
            elif cmd == CMD_STOP:
                await self._req("POST", "/printer/print/cancel")
            return True
        except Exception as e:
            print(f"[Printer {self.name}] Command failed: {e}")
            return False

    # ── File operations ────────────────────────────────────────────────────────

    async def request_file_list(self) -> bool:
        try:
            raw   = await self._req("GET", "/server/files/list")
            files = self._parse_files(raw.get("result") or [])
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": files,
            })
            return True
        except Exception as e:
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": [],
                "error": f"Could not load files: {e}",
            })
            return False

    def _parse_files(self, items) -> list:
        if not isinstance(items, list):
            return []
        result = []
        for f in items:
            if not isinstance(f, dict):
                continue
            name = f.get("path") or f.get("filename") or f.get("name") or ""
            if not name or not name.lower().endswith((".gcode", ".gco", ".g")):
                continue
            result.append({
                "name":       name,
                "path":       name,
                "size":       f.get("size") or 0,
                "is_dir":     False,
                "print_time": None,
                "layers":     None,
                "filament_g": None,
            })
        return result

    async def start_print_file(self, filename: str, print_opts: dict | None = None) -> bool:
        name = filename.lstrip("/")
        try:
            path = f"/printer/print/start?filename={urllib.parse.quote(name)}"
            await self._req("POST", path)
            return True
        except Exception as e:
            print(f"[Printer {self.name}] Start print failed: {e}")
            return False

    async def delete_file_moonraker(self, filename: str) -> bool:
        name = filename.lstrip("/")
        try:
            await self._req("DELETE", f"/server/files/gcodes/{urllib.parse.quote(name)}")
            return True
        except Exception as e:
            print(f"[Printer {self.name}] Delete failed: {e}")
            return False
