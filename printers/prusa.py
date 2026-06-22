"""
Prusa printer connection via PrusaLink HTTP API (v1).

Authenticates with X-Api-Key header (the API key set in PrusaLink settings).
Polls /api/v1/status and /api/v1/job every 2 seconds — PrusaLink has no push.
"""

import asyncio
import json
import math
import urllib.error
import urllib.request

import state
from printers.base import PrinterConnection
from printers.protocol import CMD_PAUSE, CMD_RESUME, CMD_STOP

POLL_INTERVAL = 2.0

_STATE_MAP = {
    "IDLE":      0,
    "PRINTING":  3,
    "PAUSED":    6,
    "FINISHED":  9,
    "STOPPED":   8,
    "ERROR":     14,
    "ATTENTION": 7,
    "BUSY":      1,
}


class PrusaConnection(PrinterConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, printer_type="prusa", **kwargs)
        self._base = f"http://{self.ip}"
        self._expected_filament_g = 0.0
        self._last_filename       = ""

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, body: dict | None = None):
        url  = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req  = urllib.request.Request(url, data=data, method=method)
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
        print(f"[Printer {self.name}] Connecting to PrusaLink at {self._base} …")
        fail_streak = 0
        while True:
            try:
                status_data, job_data = await asyncio.gather(
                    self._req("GET", "/api/v1/status"),
                    self._safe_job(),
                )
                fail_streak = 0
                if not self.connected:
                    self.connected = True
                    print(f"[Printer {self.name}] Connected!")
                    await self._broadcast_state()
                self._apply_status(status_data, job_data)
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

    async def _safe_job(self) -> dict:
        try:
            return await self._req("GET", "/api/v1/job")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}  # no job in progress
            raise

    # ── Status mapping ────────────────────────────────────────────────────────

    def _apply_status(self, status_data: dict, job_data: dict) -> None:
        printer = status_data.get("printer") or {}
        raw_state = (printer.get("state") or job_data.get("state") or "IDLE").upper()
        status_code = _STATE_MAP.get(raw_state, 0)

        job_file    = job_data.get("file") or {}
        filename    = job_file.get("name") or job_file.get("display_name") or ""
        time_print  = int(job_data.get("time_printing") or 0)
        time_remain = int(job_data.get("time_remaining") or 0)
        progress    = float(job_data.get("progress") or 0)

        # Fetch filament estimate from file when a new print starts
        if filename and filename != self._last_filename and status_code in (1, 3, 7):
            self._last_filename       = filename
            self._expected_filament_g = 0.0
            asyncio.get_event_loop().create_task(self._fetch_file_filament(filename))

        filament_mm = 0.0
        if self._expected_filament_g > 0 and progress > 0:
            density     = self.filament_density or 1.24
            expected_mm = self._expected_filament_g * 10.0 / (math.pi * 0.0875 ** 2 * density)
            filament_mm = (progress / 100.0) * expected_mm

        if status_code == 0:
            time_print  = 0
            time_remain = 0
            filament_mm = 0.0

        self.status = {
            "PrintInfo": {
                "Status":         status_code,
                "CurrentLayer":   0,
                "TotalLayer":     0,
                "CurrentTicks":   round(progress),
                "TotalTicks":     100,
                "PrintTime":      time_print,
                "RemainTime":     time_remain,
                "TotalExtrusion": filament_mm,
                "Filename":       filename,
            },
            "TempOfNozzle":     float(printer.get("temp_nozzle") or 0),
            "TempTargetNozzle": float(printer.get("target_nozzle") or 0),
            "TempOfHotbed":     float(printer.get("temp_bed") or 0),
            "TempTargetHotbed": float(printer.get("target_bed") or 0),
            "SpeedFactor":      int(printer.get("speed") or 100),
        }

    async def _fetch_file_filament(self, filename: str) -> None:
        try:
            for storage in ("usb", "local"):
                try:
                    meta = await self._req("GET", f"/api/v1/files/{storage}/{filename}")
                    fg = meta.get("filament") or meta.get("gcodeAnalysis", {}).get("filament")
                    if fg:
                        # PrusaLink may return filament in cm³; approximate to grams (PLA ~1.24)
                        vol = fg.get("volume") or fg.get("total", {}).get("volume")
                        wt  = fg.get("weight") or fg.get("total", {}).get("weight")
                        if wt:
                            self._expected_filament_g = float(wt)
                            print(f"[Printer {self.name}] Expected filament: {wt}g")
                            return
                        if vol:
                            self._expected_filament_g = float(vol) * (self.filament_density or 1.24)
                            return
                except urllib.error.HTTPError:
                    continue
        except Exception:
            pass

    # ── Commands ───────────────────────────────────────────────────────────────

    async def send_cmd(self, cmd, data=None) -> bool:
        if cmd == CMD_PAUSE:
            return await self._job_action("pause")
        if cmd == CMD_RESUME:
            return await self._job_action("resume")
        if cmd == CMD_STOP:
            return await self._job_delete()
        return True  # ignore unsupported commands (light, speed, etc.)

    async def _job_action(self, action: str) -> bool:
        try:
            await self._req("PUT", "/api/v1/job", {"action": action})
            return True
        except Exception as e:
            print(f"[Printer {self.name}] Job '{action}' failed: {e}")
            return False

    async def _job_delete(self) -> bool:
        try:
            await self._req("DELETE", "/api/v1/job")
            return True
        except Exception as e:
            print(f"[Printer {self.name}] Stop failed: {e}")
            return False

    # ── File operations ────────────────────────────────────────────────────────

    async def request_file_list(self) -> bool:
        try:
            raw   = await self._req("GET", "/api/v1/files/usb")
            files = self._parse_files(raw)
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

    def _parse_files(self, raw) -> list:
        if isinstance(raw, dict):
            items = raw.get("files") or raw.get("children") or []
        elif isinstance(raw, list):
            items = raw
        else:
            return []
        result = []
        for f in items:
            if not isinstance(f, dict):
                continue
            name = f.get("name") or f.get("display_name") or ""
            if not name:
                continue
            path    = f.get("path") or f"/usb/{name}"
            is_dir  = f.get("type") in ("FOLDER", "folder") or isinstance(f.get("children"), list)
            result.append({
                "name":        name,
                "path":        path,
                "size":        f.get("size") or 0,
                "is_dir":      is_dir,
                "print_time":  None,
                "layers":      None,
                "filament_g":  None,
            })
            if is_dir and isinstance(f.get("children"), list):
                result.extend(self._parse_files(f["children"]))
        return result

    async def start_print_file(self, filename: str, print_opts: dict | None = None) -> bool:
        name = filename.lstrip("/").removeprefix("usb/").removeprefix("local/")
        for storage in ("usb", "local"):
            try:
                await self._req("POST", f"/api/v1/files/{storage}/{name}", {"command": "print"})
                return True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                print(f"[Printer {self.name}] Start print error {e.code}: {e}")
                return False
            except Exception as e:
                print(f"[Printer {self.name}] Start print failed: {e}")
                return False
        print(f"[Printer {self.name}] File not found for print: {filename}")
        return False

    async def delete_file_prusa(self, filename: str) -> bool:
        name = filename.lstrip("/").removeprefix("usb/").removeprefix("local/")
        for storage in ("usb", "local"):
            try:
                await self._req("DELETE", f"/api/v1/files/{storage}/{name}")
                return True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                print(f"[Printer {self.name}] Delete error {e.code}: {e}")
                return False
        print(f"[Printer {self.name}] File not found for delete: {filename}")
        return False
