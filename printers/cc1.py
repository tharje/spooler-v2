"""
CC1 printer connection via WebSocket (SDCP protocol).
"""

import asyncio
import json

import state
from printers.base import PrinterConnection
from printers.protocol import (
    CMD_ATTRS, CMD_CAMERA, CMD_CANVAS, CMD_LIGHT, CMD_LIST_FILES,
    CMD_START, CMD_STATUS, decode_printinfo, make_msg,
)
from spoolman import spoolman_assign

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets.client import connect as ws_connect

PRINTER_PORT = 3030


class CC1Connection(PrinterConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, printer_type="cc1", **kwargs)
        self.ws = None
        self._cached_filename     = ""
        self._prev_active_tray_id = -2   # sentinel: not yet seen
        self._canvas_poll_task    = None

    async def connect(self) -> None:
        url = f"ws://{self.ip}:{PRINTER_PORT}/websocket"
        try:
            print(f"[Printer {self.name}] Connecting to {url} …")
            self.ws = await ws_connect(url, ping_interval=20, ping_timeout=20)
            self.connected = True
            print(f"[Printer {self.name}] Connected!")
            await self._broadcast_state()
            await self.send_cmd(CMD_ATTRS, {})
            await self.send_cmd(CMD_STATUS, {})
            await self.send_cmd(CMD_CAMERA, {"Enable": True})
            await self.send_cmd(CMD_CANVAS, {})
        except Exception as e:
            print(f"[Printer {self.name}] Connection failed: {e}")
            self.connected = False
            await self._broadcast_state()
            return

        self._canvas_poll_task = asyncio.create_task(self._canvas_poller())
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

    async def _canvas_poller(self) -> None:
        while True:
            await asyncio.sleep(10)
            if self.connected and self.status.get("AmsConnectStatus"):
                await self.send_cmd(CMD_CANVAS, {})

    async def start_print_file(self, filename: str) -> bool:
        if filename.startswith("/usb/"):
            prefix, bare = "/usb", filename[5:]
        elif filename.startswith("/local/"):
            prefix, bare = "/local", filename[7:]
        else:
            prefix, bare = "/local", filename
        self._cached_filename = bare
        print(f"[Printer {self.name}] CC1 start print: prefix={prefix!r} file={bare!r}")
        return await self.send_cmd(CMD_START, {
            "Filename":           bare,
            "StartLayer":         0,
            "Calibration_switch": 1,
            "PrintPlatformType":  0,
            "Tlp_Switch":         0,
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

    def _apply_canvas(self, ci: dict) -> None:
        """Store canvas_info in status and auto-assign spool on tray change."""
        self.status["canvas_info"] = ci
        active_tray = ci.get("active_tray_id", -1)
        if active_tray != self._prev_active_tray_id:
            self._prev_active_tray_id = active_tray
            spool_id = (state.tray_map.get(self.id) or {}).get(str(active_tray)) if active_tray >= 0 else None
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, spoolman_assign, self.id, spool_id)
            print(f"[Printer {self.name}] Active tray changed → {active_tray}, spool {spool_id}")

    async def _handle_message(self, raw: str) -> None:
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
                if isinstance(ci, dict):
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
            files = []
            for f in file_list:
                if not isinstance(f, dict) or not f.get("name"):
                    continue
                raw_name  = f["name"]
                full_path = raw_name if raw_name.startswith("/") else f"/local/{raw_name}"
                files.append({
                    "name":   full_path.rsplit("/", 1)[-1],
                    "path":   full_path,
                    "size":   f.get("size", 0),
                    "is_dir": f.get("type") == 0,
                })
            await state.broadcast_to_browsers({
                "type":       "file_list",
                "printer_id": self.id,
                "files":      files,
            })
            return

        await self._broadcast_state()
