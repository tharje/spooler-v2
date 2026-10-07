"""
CC1 printer connection via WebSocket (SDCP protocol).
"""

import asyncio
import json
import re
import uuid

import state
from persistence import dump_raw_message
from printers.base import PrinterConnection
from uploads import UploadError, file_md5, forward_timeout, post_multipart_file
from printers.protocol import (
    CMD_ATTRS, CMD_CAMERA, CMD_CANVAS, CMD_LIGHT, CMD_LIST_FILES,
    CMD_PAUSE, CMD_RESUME, CMD_START, CMD_STATUS, CMD_STOP, decode_printinfo, make_msg,
)
from spoolman import spoolman_assign, spoolman_set_location

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets.client import connect as ws_connect

PRINTER_PORT = 3030

# What a busy CC1 is doing, from Elegoo's SDK enums. PrintInfo.Status first
# (a print's own steps), then the machine-level CurrentStatus mode.
_PHASE_BY_PRINT_STATUS = {
    1: "Homing", 10: "Checking file", 11: "Checking printer", 15: "Leveling",
    16: "Heating", 17: "Resonance test", 18: "Starting print", 19: "Leveling",
    20: "Heating", 21: "Homing", 22: "Resonance test",
    23: "Loading filament", 24: "Unloading filament",
    25: "Filament unload problem", 26: "Filament unload paused",
}
_PHASE_BY_MACHINE_MODE = {
    4: "Self-check", 5: "Leveling", 6: "Resonance test", 7: "Busy",
    8: "Checking file", 9: "Homing", 10: "Unloading filament", 11: "PID calibration",
}
# Rejected-command reasons (Data.Ack), from Elegoo's SDK; 0 means accepted.
_ACK_MESSAGES = {
    1: "the printer is busy or refused the command",
    2: "the file wasn't found on the printer",
    3: "the file failed the printer's MD5 check",
    4: "the printer couldn't read the file",
    5: "the file's format or resolution doesn't match the printer",
    6: "the file is for a different printer model",
}
_CMD_NAMES = {128: "Start print", 129: "Pause", 130: "Stop", 131: "Resume"}

_TIME_RE = re.compile(r'_(?:(\d+)h)?(\d+)m(?:(\d+)s)?\.[^.]+$')

def _parse_print_time(filename: str) -> int | None:
    """Extract estimated print time in seconds from Elegoo slicer filename."""
    m = _TIME_RE.search(filename)
    if m:
        h    = int(m.group(1) or 0)
        mins = int(m.group(2))
        secs = int(m.group(3) or 0)
        total = h * 3600 + mins * 60 + secs
        return total if total > 0 else None
    return None


class CC1Connection(PrinterConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, printer_type="cc1", **kwargs)
        self.ws = None
        self._cached_filename     = ""
        self._prev_active_tray_id = -2   # sentinel: not yet seen
        self._canvas_poll_task    = None
        self._warned_slots        = False

    async def connect(self) -> None:
        self._prev_active_tray_id = -2
        url = f"ws://{self.ip}:{PRINTER_PORT}/websocket"
        try:
            print(f"[Printer {self.name}] Connecting to {url} …")
            # Disable WS-level pings — CC1 firmware doesn't respond to them,
            # which causes spurious disconnects. App-level keepalive handles this.
            self.ws = await ws_connect(url, ping_interval=None)
            self.connected = True
            self._warned_slots = False
            print(f"[Printer {self.name}] Connected!")
            await self._broadcast_state()
            await self.send_cmd(CMD_ATTRS, {})
            await self.send_cmd(CMD_STATUS, {})
            await self.send_cmd(CMD_CAMERA, {"Enable": True})
            await self.send_cmd(CMD_CANVAS, {})
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._sync_spoolman_locations)
        except Exception as e:
            print(f"[Printer {self.name}] Connection failed: {e}")
            if " 500" in str(e):
                # The CC1 accepts at most 5 WebSocket clients and answers the
                # 6th with HTTP 500 "too many client" (reported by pycentauri).
                print(f"[Printer {self.name}] HTTP 500 usually means the printer's 5 connection "
                      f"slots are taken (slicer, Elegoo app, web UI, Home Assistant ...). "
                      f"Close one of them and Spooler will connect on the next retry.")
                if not self._warned_slots:
                    self._warned_slots = True
                    await state.broadcast_to_browsers({
                        "type": "error",
                        "message": f"{self.name}: the printer refused the connection — all 5 of its "
                                   f"connection slots may be in use (slicer, Elegoo app, web UI, Home Assistant).",
                    })
            self.connected = False
            await self._broadcast_state()
            return

        self._canvas_poll_task = asyncio.create_task(self._keepalive_poller())
        try:
            async for raw in self.ws:
                await self._handle_message(raw)
        except Exception as e:
            print(f"[Printer {self.name}] Disconnected: {e}")
        finally:
            if self._canvas_poll_task:
                self._canvas_poll_task.cancel()
                try:
                    await self._canvas_poll_task
                except asyncio.CancelledError:
                    pass
            self.connected = False
            self.ws = None
            await self._broadcast_state()

    def _update_phase(self) -> None:
        pi = decode_printinfo(self.status.get("PrintInfo") or {}) if isinstance(self.status.get("PrintInfo"), dict) else {}
        label = _PHASE_BY_PRINT_STATUS.get(pi.get("Status"), "")
        if not label and pi.get("Status") in (0, None):
            arr = self.status.get("CurrentStatus")
            if isinstance(arr, list) and arr:
                mode = arr[1] if arr[0] == 2 and len(arr) > 1 else arr[0]
                label = _PHASE_BY_MACHINE_MODE.get(mode, "")
        self.phase = label

    def _protocol_reason_hint(self) -> dict | None:
        # Nothing in Elegoo's SDK or the SDCP spec tells us where (or whether)
        # CC1 reports the codes its screen shows (101, 304, ...; see
        # printers/error_codes.py CC1_ERROR_CODES), so only what the status
        # itself says is used: PrintInfo.Status 14 means "stopped because of an
        # error", and a non-zero ErrorNumber (SDCP spec field) is kept raw.
        pi = decode_printinfo(self.status.get("PrintInfo") or {}) if isinstance(self.status.get("PrintInfo"), dict) else {}
        err = pi.get("ErrorNumber")
        if pi.get("Status") != 14 and not err:
            return None
        return {
            "code":     err if err else "",
            "category": "unknown",
            "message":  ("The printer stopped the print because of an error. "
                         "The details are shown on the printer's screen.") if pi.get("Status") == 14 else "",
            "action":   "",
            "raw":      {"Status": pi.get("Status"), "ErrorNumber": err,
                         "CurrentStatus": self.status.get("CurrentStatus")},
        }

    async def _keepalive_poller(self) -> None:
        while True:
            await asyncio.sleep(20)
            if not self.connected:
                break
            await self.send_cmd(CMD_STATUS, {})
            if self.status.get("AmsConnectStatus"):
                await self.send_cmd(CMD_CANVAS, {})

    supports_upload = True
    supports_light = True

    async def set_light(self, on: bool) -> bool:
        return await self.send_cmd(CMD_LIGHT, {"LightStatus": {"SecondLight": bool(on), "RgbLight": [0, 0, 0]}})

    async def upload_file(self, local_path, remote_name: str, start_after: bool = False) -> bool:
        # UNVERIFIED against real hardware: single-chunk variant of the SDCP
        # v3 upload form (S-File-MD5/Check/Offset/Uuid/TotalSize/File). See
        # the T9 test instruction before relying on this.
        size = local_path.stat().st_size
        loop = asyncio.get_running_loop()

        def _send():
            fields = {
                "S-File-MD5": file_md5(local_path),
                "Check": 1,
                "Offset": 0,
                "Uuid": uuid.uuid4().hex,
                "TotalSize": size,
            }
            return post_multipart_file(self.ip, 80, "/uploadFile/upload", fields, "File",
                                       remote_name, local_path, timeout=forward_timeout(size))
        try:
            status, body = await loop.run_in_executor(None, _send)
        except OSError as e:
            raise UploadError(f"Could not reach the printer: {e}") from e
        text = body.decode("utf-8", errors="replace")[:300]
        print(f"[Printer {self.name}] CC1 upload -> HTTP {status}: {text}")
        if status != 200:
            raise UploadError(f"Printer rejected the upload (HTTP {status}): {text}")
        if start_after:
            return await self.start_print_file(remote_name)
        return True

    async def start_print_file(self, filename: str, print_opts: dict | None = None) -> bool:
        if filename.startswith("/usb/"):
            prefix, bare = "/usb", filename[5:]
        elif filename.startswith("/local/"):
            prefix, bare = "/local", filename[7:]
        else:
            prefix, bare = "/local", filename
        self._cached_filename = bare
        opts = print_opts or {}
        print(f"[Printer {self.name}] CC1 start print: prefix={prefix!r} file={bare!r} opts={opts}")
        return await self.send_cmd(CMD_START, {
            "Filename":           bare,
            "StartLayer":         0,
            "Calibration_switch": 1 if opts.get("leveling")     else 0,
            "PrintPlatformType":  1 if opts.get("smooth_plate") else 0,
            "Tlp_Switch":         1 if opts.get("timelapse")    else 0,
            "slot_map":           [],
            "path_prefix":        prefix,
        })

    async def send_cmd(self, cmd: int, data: dict) -> bool:
        if not self.ws or not self.connected:
            return False
        msg = make_msg(cmd, data, self.mainboard_id)
        try:
            await self.ws.send(json.dumps(msg))
            return True
        except Exception as e:
            print(f"[Printer {self.name}] Send error: {e}")
            return False

    def _sync_spoolman_locations(self) -> None:
        for spool_id in (state.tray_map.get(self.id) or {}).values():
            if spool_id is not None:
                spoolman_set_location(spool_id, self.id)

    def _apply_canvas(self, ci: dict) -> None:
        """Store canvas_info in status and auto-assign spool on tray change."""
        self.status["canvas_info"] = ci
        active_tray = ci.get("active_tray_id", -1)
        if active_tray != self._prev_active_tray_id:
            self._prev_active_tray_id = active_tray
            spool_id = (state.tray_map.get(self.id) or {}).get(str(active_tray)) if active_tray >= 0 else None
            loop = asyncio.get_running_loop()
            if active_tray >= 0:
                loop.run_in_executor(None, spoolman_assign, self.id, spool_id)
            self._on_tray_change(spool_id)
            print(f"[Printer {self.name}] Active tray changed → {active_tray}, spool {spool_id}")

    async def _handle_message(self, raw: str) -> None:
        dump_raw_message(self.id, "cc1_ws", raw)
        try:
            msg = json.loads(raw)
        except Exception:
            return

        if "Status" in msg and isinstance(msg["Status"], dict):
            prev_canvas = self.status.get("canvas_info")
            self.status = msg["Status"]
            # Preserve canvas_info — it comes from CMD_CANVAS, not status pushes
            if prev_canvas and "canvas_info" not in self.status:
                self.status["canvas_info"] = prev_canvas
            if self._cached_filename and isinstance(self.status.get("PrintInfo"), dict):
                pi_decoded = decode_printinfo(self.status["PrintInfo"])
                if not pi_decoded.get("Filename"):
                    self.status["PrintInfo"]["Filename"] = self._cached_filename
            self._update_phase()
            await self._check_print_transition()
            await self._broadcast_state()
            return

        if "Attributes" in msg and isinstance(msg["Attributes"], dict):
            self.attrs = msg["Attributes"]
            mbid = self.attrs.get("MainboardID")
            if mbid and not self.mainboard_id:
                self.mainboard_id = mbid
            await self._broadcast_state()
            return

        data    = msg.get("Data", {})
        cmd     = data.get("Cmd")
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
                self._update_phase()
                # This is the keepalive poller's CMD_STATUS response, not the
                # printer's unsolicited "Status" push handled above — it still
                # carries fresh PrintInfo, so transitions must be checked here
                # too or a print start/end seen only via polling is missed.
                await self._check_print_transition()
        elif cmd in (CMD_START, CMD_PAUSE, CMD_STOP, CMD_RESUME):
            ack = payload.get("Ack") if isinstance(payload, dict) else None
            if ack not in (0, None):
                why = _ACK_MESSAGES.get(ack, f"error code {ack}")
                print(f"[Printer {self.name}] CC1 command {cmd} rejected, Ack={ack}")
                await state.broadcast_to_browsers({
                    "type": "error",
                    "message": f"{self.name}: {_CMD_NAMES.get(cmd, 'Command')} was rejected — {why}.",
                })
        elif cmd == CMD_CAMERA:
            url = payload.get("VideoUrl") or payload.get("Url")
            if url:
                self.camera_url = url if url.startswith("http") else f"http://{url}"
        elif cmd == CMD_LIGHT:
            await self.send_cmd(CMD_STATUS, {})
        elif cmd == CMD_CANVAS:
            ack = payload.get("Ack", 0)
            if ack != 0:
                print(f"[Printer {self.name}] Canvas cmd error Ack={ack}")
            else:
                # Response may wrap data under a key or be flat
                ci = (payload.get("canvas_info")
                      or payload.get("CanvasInfo")
                      or payload.get("AmsInfo"))
                if ci is None and "canvas_list" in payload:
                    ci = payload  # flat — the payload IS the canvas_info
                if isinstance(ci, dict) and ci.get("canvas_list"):
                    self._apply_canvas(ci)
                    if state.DEBUG:
                        print(f"[Printer {self.name}] Canvas: {len(ci.get('canvas_list', []))} canvas(es), "
                              f"active_tray={ci.get('active_tray_id', -1)}")
                else:
                    if state.DEBUG:
                        print(f"[Printer {self.name}] Canvas cmd 324 raw payload: {json.dumps(payload)[:400]}")
        elif cmd == CMD_LIST_FILES:
            file_list = payload.get("FileList") or []
            if file_list and all(f.get("type") == 0 for f in file_list):
                await self.send_cmd(CMD_LIST_FILES, {"Url": "/local", "IsDir": True})
                return
            if state.DEBUG and file_list:
                print(f"[CC1 file_list] first item keys: {list(file_list[0].keys())}")
                print(f"[CC1 file_list] first item: {json.dumps(file_list[0])[:400]}")
            files = []
            for f in file_list:
                if not isinstance(f, dict) or not f.get("name"):
                    continue
                raw_name  = f["name"]
                full_path = raw_name if raw_name.startswith("/") else f"/local/{raw_name}"
                filament_mm = f.get("EstFilamentLength") or 0
                bare_name   = full_path.rsplit("/", 1)[-1]
                files.append({
                    "name":        bare_name,
                    "path":        full_path,
                    "size":        f.get("FileSize", 0),
                    "is_dir":      f.get("type") == 0,
                    "print_time":  _parse_print_time(bare_name),
                    "layers":      f.get("TotalLayers"),
                    "filament_mm": filament_mm if filament_mm else None,
                    "filament_g":  None,
                })
            await state.broadcast_to_browsers({
                "type":       "file_list",
                "printer_id": self.id,
                "files":      files,
            })
            return

        await self._broadcast_state()
