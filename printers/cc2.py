"""
CC2 printer connection via MQTT (Klipper-based firmware).
"""

import asyncio
import json
import secrets
import time
import uuid
from pathlib import Path

import state
from printers.base import PrinterConnection
from spoolman import spoolman_assign
from printers.protocol import (
    CMD_LIGHT, CMD_PAUSE, CMD_RESUME, CMD_STOP,
    deep_merge,
)

try:
    import aiomqtt
    AIOMQTT_AVAILABLE = True
except ImportError:
    AIOMQTT_AVAILABLE = False


def _serial_cache_path(printer_id: str) -> Path:
    from persistence import DATA_DIR
    return DATA_DIR / f"cc2_serial_{printer_id}.txt"

def _load_cached_serial(printer_id: str) -> str | None:
    try:
        v = _serial_cache_path(printer_id).read_text().strip()
        return v or None
    except FileNotFoundError:
        return None

def _save_cached_serial(printer_id: str, serial: str) -> None:
    try:
        _serial_cache_path(printer_id).write_text(serial)
    except Exception:
        pass

# Official CC2 method codes (elegooofficial/CentauriCarbon2 method.h)
_CC2_METHODS = {
    CMD_PAUSE:  1021,
    CMD_STOP:   1022,
    CMD_RESUME: 1023,
    CMD_LIGHT:  1029,
}

_CC2_STATE_KEYS = {
    "machine_status", "print_status", "extruder",
    "heater_bed", "ztemperature_sensor", "gcode_move", "led",
    "external_device", "tool_head", "fans",
    # Canvas / filament
    "canvas", "canvas_info", "channel_info", "channels",
    "filament", "filament_info", "extruder_filament",
    "mmu", "ams",
}


class CC2Connection(PrinterConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, printer_type="cc2", **kwargs)
        self._mqtt_client      = None
        self._mqtt_serial: str | None = None
        self._mqtt_client_id: str | None = None
        self._mqtt_request_id: str | None = None
        self._mqtt_registered  = False
        self._cc2_state: dict  = {}
        self._filament_mm_max  = 0.0
        self._prev_state_str   = ""
        self._extruder_offset  = 0.0
        self._last_extruder    = 0.0
        self._awaiting_file_list  = False
        self._current_filename    = ""
        self._mqtt_serial         = _load_cached_serial(self.id)
        self._prev_active_tray_id = -2  # sentinel: not yet seen

    async def connect(self) -> None:
        if not AIOMQTT_AVAILABLE:
            print(f"[Printer {self.name}] aiomqtt not installed — CC2 unavailable")
            await self._broadcast_state()
            return
        try:
            print(f"[Printer {self.name}] Connecting via MQTT to {self.ip}:1883 …")
            ts_hex  = format(int(time.time() * 1000), "x")[-5:]
            rnd_hex = format(secrets.randbelow(4096), "x")
            self._mqtt_client_id  = f"0cli{ts_hex}{rnd_hex}"[:10]
            self._mqtt_request_id = uuid.uuid4().hex[:16]
            self._mqtt_registered = False
            # Keep any cached serial from __init__; cleared only on explicit reset

            async with aiomqtt.Client(
                hostname=self.ip,
                port=1883,
                username="elegoo",
                password=self.access_code,
            ) as client:
                self._mqtt_client = client
                await client.subscribe("elegoo/+/+/register_response")
                self.camera_url = f"http://{self.ip}:8080/mjpeg"

                if self._mqtt_serial:
                    # Fast path: serial known from cache — subscribe only to what we need
                    sn = self._mqtt_serial
                    await client.subscribe(f"elegoo/{sn}/api_status")
                    await client.subscribe(f"elegoo/{sn}/{self._mqtt_client_id}/api_response")
                    await client.publish(
                        f"elegoo/{sn}/api_register",
                        json.dumps({
                            "client_id":  self._mqtt_client_id,
                            "request_id": self._mqtt_request_id,
                        }),
                    )
                    print(f"[Printer {self.name}] MQTT open — registration sent (cached SN {sn})")
                else:
                    # Cold start: subscribe to everything so the VERY FIRST message from
                    # this broker (whatever it is) reveals the serial number immediately.
                    await client.subscribe("elegoo/#")
                    print(f"[Printer {self.name}] MQTT open — listening for serial (cold start)…")
                poll_task = asyncio.create_task(self._mqtt_status_poller())
                try:
                    async for message in client.messages:
                        await self._handle_mqtt_message(message)
                finally:
                    poll_task.cancel()
                    try:
                        await poll_task
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Printer {self.name}] MQTT failed: {e}")
        finally:
            self._mqtt_client     = None
            self._mqtt_registered = False
            self.connected        = False
            self.camera_url       = f"http://{self.ip}:8080/mjpeg"
            await self._broadcast_state()

    async def send_cmd(self, cmd: int, data: dict | None = None) -> bool:
        if not self._mqtt_client or not self._mqtt_serial:
            return False
        if isinstance(cmd, int) and cmd > 1000:
            method = cmd  # already a CC2 method code
        else:
            method = _CC2_METHODS.get(cmd)
            if not method:
                return False
        if not self._mqtt_registered and method != 1003:
            if state.DEBUG:
                print(f"[Printer {self.name}] CC2 not registered yet, dropping method {method}")
            return False
        topic   = f"elegoo/{self._mqtt_serial}/{self._mqtt_client_id}/api_request"
        payload = {"id": uuid.uuid4().int & 0xFFFF, "method": method}
        if cmd == CMD_LIGHT and data:
            light_on = data.get("LightStatus", {}).get("SecondLight", False)
            payload["params"] = {"power": 1 if light_on else 0}
        elif method in (1020, 1031, 1044, 1047) and data:
            payload["params"] = data
        try:
            await self._mqtt_client.publish(topic, json.dumps(payload))
            return True
        except Exception as e:
            print(f"[Printer {self.name}] MQTT send error: {e}")
            return False

    async def _mqtt_status_poller(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(5)
            if self._mqtt_registered:
                await self.send_cmd(1003)   # machine_status
                if tick % 2 == 0:
                    await self.send_cmd(2005)  # canvas channel info
                tick += 1

    async def _handle_mqtt_message(self, message) -> None:
        topic = str(message.topic)

        if "register_response" in topic:
            try:
                p = json.loads(message.payload.decode())
                if p.get("error") == "ok":
                    self._mqtt_registered = True
                    self.connected = True
                    print(f"[Printer {self.name}] CC2 registered OK — ready")
                    await self._broadcast_state()
                    await self.send_cmd(1002)  # full state
                    await self.send_cmd(1003)  # machine_status
                    await self.send_cmd(1042)  # camera URL
                    await self.send_cmd(2005)  # canvas channel info
                    await self.send_cmd(1056)  # extruder filament info
                else:
                    print(f"[Printer {self.name}] CC2 registration failed: {p}")
            except Exception:
                pass
            return

        try:
            payload = json.loads(message.payload.decode())
        except Exception:
            return

        if "api_response" in topic:
            inner  = payload.get("result")

            if self._awaiting_file_list and isinstance(inner, dict) and "file_list" in inner:
                self._awaiting_file_list = False
                files = [
                    {
                        "name":   f.get("filename", ""),
                        "path":   f.get("filename", ""),
                        "size":   f.get("size", 0),
                        "is_dir": f.get("type") == "dir",
                    }
                    for f in (inner.get("file_list") or [])
                    if isinstance(f, dict)
                ]
                await state.broadcast_to_browsers({
                    "type": "file_list", "printer_id": self.id, "files": files,
                })
                return

            # Camera URL response (method 1042 GET_MONITOR_VIDENO_URL)
            _method = payload.get("method")
            if _method == 1042 and isinstance(inner, dict):
                url = inner.get("url") or inner.get("video_url") or inner.get("mjpeg_url")
                if url:
                    self.camera_url = url
                    await self._broadcast_state()
                return

            source = inner if isinstance(inner, dict) else payload
            if state.DEBUG:
                _method = payload.get("method")
                if _method in (2005, 1056, 1044):
                    print(f"[CC2 probe] method={_method} full response: "
                          f"{json.dumps(payload)[:800]}")
                else:
                    unknown = {k for k in source if k not in _CC2_STATE_KEYS
                               and k not in ("error_code",) and isinstance(source[k], (dict, list))}
                    if unknown:
                        print(f"[CC2] method={_method} unknown keys: {unknown} — "
                              f"raw: {json.dumps({k: source[k] for k in unknown})[:600]}")
            updates = {k: v for k, v in source.items()
                       if k in _CC2_STATE_KEYS and isinstance(v, dict)}
            # Strip stale filament_used from the 1002 full-state snapshot
            if inner is not None and isinstance(updates.get("print_status"), dict):
                updates["print_status"].pop("filament_used", None)
            if updates:
                deep_merge(self._cc2_state, updates)
                self._apply_cc2_status()
                await self._broadcast_state()
            return

        # Cold-start serial discovery: extract SN from any elegoo/{sn}/... topic
        if not self._mqtt_serial and not self._mqtt_registered:
            parts = topic.split("/")
            if len(parts) >= 3 and parts[0] == "elegoo" and parts[1]:
                sn = parts[1]
                self._mqtt_serial = sn
                _save_cached_serial(self.id, sn)
                print(f"[Printer {self.name}] SN discovered: {sn} (saved to cache)")
                if self._mqtt_client:
                    # Switch from wildcard to specific subscriptions
                    await self._mqtt_client.unsubscribe("elegoo/#")
                    await self._mqtt_client.subscribe(f"elegoo/{sn}/api_status")
                    await self._mqtt_client.subscribe(
                        f"elegoo/{sn}/{self._mqtt_client_id}/api_response"
                    )
                    await self._mqtt_client.publish(
                        f"elegoo/{sn}/api_register",
                        json.dumps({
                            "client_id":  self._mqtt_client_id,
                            "request_id": self._mqtt_request_id,
                        }),
                    )
                    print(f"[Printer {self.name}] CC2 registration sent")

        result = payload.get("result", {})
        if not isinstance(result, dict) or not result:
            return

        deep_merge(self._cc2_state, result)
        self._apply_cc2_status()
        await self._check_print_transition()
        await self._broadcast_state()

    async def request_file_list(self) -> bool:
        if not self._mqtt_registered:
            msg = ("Printer MQTT not ready yet — wait a moment and try again."
                   if self.connected else "Printer not connected.")
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": [],
                "error": msg,
            })
            return False
        self._awaiting_file_list = True
        ok = await self.send_cmd(1044, {"storage_media": "local", "offset": 0, "limit": 50})
        if not ok:
            self._awaiting_file_list = False
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": [],
                "error": "Failed to send file list request.",
            })
        else:
            asyncio.create_task(self._file_list_timeout())
        return ok

    async def _file_list_timeout(self) -> None:
        await asyncio.sleep(10)
        if self._awaiting_file_list:
            self._awaiting_file_list = False
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": [],
                "error": "File list request timed out.",
            })

    async def start_print_file(self, filename: str) -> bool:
        self._current_filename = filename
        return await self.send_cmd(1020, {"filename": filename, "storage_media": "local"})

    def _apply_cc2_status(self) -> None:
        s     = self._cc2_state
        ps    = s.get("print_status", {})
        gm    = s.get("gcode_move", {})
        ext   = s.get("extruder", {})
        bed   = s.get("heater_bed", {})
        ztemp = s.get("ztemperature_sensor", {})
        ms    = s.get("machine_status", {})

        print_duration = ps.get("print_duration", 0) or 0
        remaining      = ps.get("remaining_time_sec", 0) or 0
        state_str      = ps.get("state", "")
        filename_from_ps = (ps.get("filename") or ps.get("task_name") or
                            ps.get("file_name") or ps.get("file", {}).get("filename", ""))
        if filename_from_ps:
            self._current_filename = filename_from_ps
        sub_status     = ms.get("sub_status", 0)

        _SUB_TRANSIENT = {2501: 5, 2503: 7}
        _SUB_STABLE    = {
            1045: 15, 1096: 15, 1405: 15,
            2075: 3,  2401: 3,  2402: 3,
            2077: 9,
            2502: 6,  2505: 6,
            2504: 8,
        }
        _STATE_STR = {
            "printing":  3,
            "paused":    6,
            "complete":  9,
            "cancelled": 8,
            "error":     14,
            "standby":   0,
        }

        if sub_status in _SUB_TRANSIENT:
            status_code = _SUB_TRANSIENT[sub_status]
        elif state_str in _STATE_STR:
            status_code = _STATE_STR[state_str]
        elif sub_status in _SUB_STABLE:
            status_code = _SUB_STABLE[sub_status]
        elif remaining > 0:
            status_code = 3
        else:
            status_code = 0

        if status_code == 0:
            print_duration = 0
            remaining      = 0

        total    = print_duration + remaining
        progress = min(100, round(print_duration / total * 100)) if total > 0 else 0

        if state_str == "printing" and self._prev_state_str not in ("printing", "paused"):
            self._filament_mm_max  = 0.0
            self._extruder_offset  = 0.0
            self._last_extruder    = 0.0
        if state_str:
            self._prev_state_str = state_str

        filament_from_push = ps.get("filament_used") or 0
        if filament_from_push > self._filament_mm_max:
            self._filament_mm_max = filament_from_push

        raw_ext = gm.get("extruder") or 0
        if raw_ext < self._last_extruder * 0.5 and self._last_extruder > 1.0:
            self._extruder_offset += self._last_extruder
        self._last_extruder = raw_ext

        filament_mm = max(self._filament_mm_max, self._extruder_offset + raw_ext)

        led    = s.get("led", {})
        led_on = 1 if (led.get("status", 0) or 0) > 0 else 0

        speed_factor = gm.get("speed_factor", 1.0) or 1.0

        self.status = {
            "PrintInfo": {
                "Status":         status_code,
                "CurrentLayer":   ps.get("current_layer", 0),
                "TotalLayer":     ps.get("total_layer", 0),
                "CurrentTicks":   progress,
                "TotalTicks":     100,
                "PrintTime":      print_duration,
                "RemainTime":     remaining,
                "TotalExtrusion": filament_mm,
                "Filename":       self._current_filename,
            },
            "TempOfNozzle":     ext.get("temperature", 0),
            "TempTargetNozzle": ext.get("target", 0),
            "TempOfHotbed":     bed.get("temperature", 0),
            "TempTargetHotbed": bed.get("target", 0),
            "TempOfBox":        ztemp.get("temperature", 0),
            "LightStatus":      {"SecondLight": led_on},
            "SpeedFactor":      round(speed_factor * 100),
        }

        # Expose canvas tray info so the browser can render filament slots
        ci = s.get("canvas_info", {})
        if ci:
            self.status["canvas_info"] = ci
            active_tray = ci.get("active_tray_id", -1)
            if active_tray != self._prev_active_tray_id:
                self._prev_active_tray_id = active_tray
                spool_id = (state.tray_map.get(self.id) or {}).get(str(active_tray)) if active_tray >= 0 else None
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, spoolman_assign, self.id, spool_id)
                print(f"[Printer {self.name}] Active tray changed → {active_tray}, spool {spool_id}")
