"""
CC2 printer connection via MQTT (Klipper-based firmware).
"""

import asyncio
import json
import math
import secrets
import time
import uuid
from pathlib import Path

import state
from persistence import dump_raw_message
from printers.base import PrinterConnection
from printers.error_codes import is_api_result_code, lookup as lookup_error_code
from spoolman import spoolman_assign, spoolman_set_location
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
    # {"exception_code": {"<code>": {"time": ...}}} -- when each fault was raised
    "exception",
    # Canvas / filament
    "canvas", "canvas_info", "channel_info", "channels",
    "filament", "filament_info", "extruder_filament",
    "mmu", "ams",
    # Device attributes (method 1001 response)
    "software_version",
}


# machine_status.status values that mean "busy, but not printing" -> the
# Spooler status codes the "preparing" display state is built from.
# 3/4 filament load/unload, 5 auto-leveling, 6 PID, 7 resonance test,
# 8 self-check, 10 homing, 13 extruder maintenance.
_MS_PREPARING = {3: 10, 4: 10, 5: 20, 6: 10, 7: 10, 8: 10, 10: 10, 13: 10}
# 0 starting, 1 idle, 2 printing (handled by print_status), 9 updating,
# 11 file transfer, 12 timelapse export: shown as before, no log noise.
_MS_KNOWN_IDLE_OR_HANDLED = {0, 1, 2, 9, 11, 12}
_logged_ms_status: set = set()
_logged_sub_status: set = set()
# sub_status values that mean material is (or is about to be) laid down.
_EXTRUDING_SUBS = {2075, 2401, 2402, 2077, 2501, 2502, 2503, 2504, 2505}

# What a busy-but-not-printing printer is doing, from sub_status (Elegoo
# elegoo-link SDK codes) or, failing that, machine_status.status.
_PHASE_BY_SUB = {
    1045: "Heating nozzle", 1096: "Heating nozzle",
    1405: "Heating bed",    1906: "Heating bed",
    2801: "Homing", 2802: "Homing", 2803: "Homing failed",
    2901: "Leveling", 2902: "Leveling",
    1133: "Loading filament", 1134: "Loading filament", 1135: "Loading filament",
    1136: "Filament loaded",
    1143: "Unloading filament", 1144: "Unloading filament", 1145: "Filament unloaded",
    1061: "Loading filament", 1063: "Filament loaded",
    1062: "Unloading filament", 1064: "Filament unloaded",
    1503: "PID calibration", 1504: "PID calibration",
    5934: "Resonance test",
}
_PHASE_BY_MS = {
    3: "Filament change", 4: "Filament change", 5: "Leveling", 6: "PID calibration",
    7: "Resonance test", 8: "Self-check", 10: "Homing", 13: "Extruder maintenance",
    14: "Emergency stop", 15: "Recovering after power loss",
}


def _cc2_phase(ms_status, sub_status, status_code) -> str:
    # Only meaningful for pre-print/maintenance work and the odd modes that
    # have no print_status of their own; during plain printing it stays empty.
    # While printing/paused, only the sub_status phases are shown (e.g. a
    # nozzle re-heat during a filament swap), never the machine mode.
    if status_code in (3, 5, 6, 9, 0) and ms_status not in _PHASE_BY_MS:
        return _PHASE_BY_SUB.get(sub_status, "") if status_code == 3 else ""
    return _PHASE_BY_SUB.get(sub_status) or _PHASE_BY_MS.get(ms_status, "")


# Requests (polled every 5 s) without any answer before we register again.
UNANSWERED_LIMIT = 6


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
        self._unanswered          = 0  # requests sent since the printer last answered this client
        self._prev_active_tray_id = -2  # sentinel: not yet seen
        self._pending_thumb_fut: asyncio.Future | None = None
        self._pending_meta_fut:  asyncio.Future | None = None
        self._expected_filament_g    = 0.0  # from method 1046 at print start
        self._expected_print_time_s  = 0    # from method 1046 at print start
        self._last_active_filament_mm = 0.0 # snapshot for cancelled prints
        self._meta_fetch_filename    = ""   # avoid duplicate 1046 fetches

    async def connect(self) -> None:
        self._prev_active_tray_id = -2
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
            payload["params"] = {"brightness": 255 if light_on else 0, "power": 1 if light_on else 0}
        elif method in (1020, 1031, 1044, 1045, 1046, 1047) and data:
            payload["params"] = data
        try:
            await self._mqtt_client.publish(topic, json.dumps(payload))
            self._unanswered += 1
            return True
        except Exception as e:
            print(f"[Printer {self.name}] MQTT send error: {e}")
            return False

    async def _reregister(self) -> None:
        self._mqtt_registered = False
        self._unanswered = 0
        self._mqtt_request_id = uuid.uuid4().hex[:16]
        try:
            await self._mqtt_client.publish(
                f"elegoo/{self._mqtt_serial}/api_register",
                json.dumps({"client_id": self._mqtt_client_id, "request_id": self._mqtt_request_id}),
            )
        except Exception as e:
            print(f"[Printer {self.name}] MQTT re-register error: {e}")

    async def _mqtt_status_poller(self) -> None:
        # Full state (1002), not just machine_status (1003) -- the printer's
        # unsolicited api_status pushes (method 6000) are a delta stream, and
        # Elegoo's own client resyncs via 1002 whenever it detects a gap in
        # that stream. We don't track delta continuity, so poll 1002 on a
        # timer instead: without it, a single dropped delta (e.g. the one
        # that would clear print_status.state from "complete" back to "" /
        # idle after the user confirms on the printer) leaves Spooler showing
        # a stale status forever, since nothing else ever re-requests it.
        # Verified live (2026-10-06): 1002's response is a strict superset of
        # 1003's (same machine_status block, plus print_status/extruder/etc).
        tick = 0
        unreg = 0
        while True:
            await asyncio.sleep(5)
            if self._mqtt_registered and self._unanswered >= UNANSWERED_LIMIT:
                # The printer has stopped answering us although its status
                # broadcasts still arrive: our registration is gone (the printer
                # only keeps a few clients). Commands such as the light would be
                # ignored silently, so register again.
                print(f"[Printer {self.name}] No answer to {self._unanswered} requests — registering again")
                await self._reregister()
            elif not self._mqtt_registered and self._mqtt_serial and self._mqtt_client:
                unreg += 1
                if unreg >= UNANSWERED_LIMIT:   # registration never completed: ask again
                    unreg = 0
                    await self._reregister()
            elif self._mqtt_registered:
                unreg = 0
                await self.send_cmd(1002)   # full state (includes machine_status)
                if tick % 2 == 0:
                    await self.send_cmd(2005)  # canvas channel info
                tick += 1

    async def _handle_mqtt_message(self, message) -> None:
        topic = str(message.topic)
        dump_raw_message(self.id, f"cc2_mqtt:{topic}", message.payload)
        # Only what the printer sent counts. With the cold-start wildcard
        # subscription the broker also echoes our own requests back.
        if topic.endswith(("/api_status", "/api_response", "/register_response")):
            self._mark_seen()

        if "register_response" in topic:
            try:
                p = json.loads(message.payload.decode())
                # We subscribe with a wildcard, so this may be ANOTHER client's
                # (the Elegoo app, a slicer) registration. Only our own counts.
                if p.get("client_id") not in (None, self._mqtt_client_id):
                    return
                if p.get("error") == "ok":
                    self._mqtt_registered = True
                    self.connected = True
                    print(f"[Printer {self.name}] CC2 registered OK — ready")
                    await self._broadcast_state()
                    await self.send_cmd(1001)  # device attributes (model/firmware/sn)
                    await self.send_cmd(1002)  # full state
                    await self.send_cmd(1003)  # machine_status
                    await self.send_cmd(1042)  # camera URL
                    await self.send_cmd(2005)  # canvas channel info
                    await self.send_cmd(1056)  # extruder filament info
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(None, self._sync_spoolman_locations)
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
            self._unanswered = 0
            inner  = payload.get("result")

            if self._awaiting_file_list and isinstance(inner, dict) and "file_list" in inner:
                self._awaiting_file_list = False
                raw_list = inner.get("file_list") or []
                if state.DEBUG and raw_list:
                    print(f"[CC2 file_list] first item keys: {list(raw_list[0].keys())}")
                    print(f"[CC2 file_list] first item: {json.dumps(raw_list[0])[:400]}")
                from printers.cc1 import _parse_print_time
                files = [
                    {
                        "name":       f.get("filename", ""),
                        "path":       f.get("filename", ""),
                        "size":       f.get("size", 0),
                        "is_dir":     f.get("type") == "dir",
                        "print_time": f.get("print_time") or _parse_print_time(f.get("filename", "")),
                        "layers":     f.get("layer"),
                        "filament_g": f.get("total_filament_used"),
                        "color_map":  f.get("color_map"),
                        "printed":    f.get("total_print_times", 0),
                    }
                    for f in raw_list
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

            # Thumbnail response (method 1045)
            if _method == 1045 and isinstance(inner, dict):
                if self._pending_thumb_fut and not self._pending_thumb_fut.done():
                    b64 = inner.get("thumbnail") or inner.get("data") or inner.get("image")
                    self._pending_thumb_fut.set_result(b64 if isinstance(b64, str) else None)
                return

            # File metadata response (method 1046)
            if _method == 1046 and isinstance(inner, dict):
                if self._pending_meta_fut and not self._pending_meta_fut.done():
                    self._pending_meta_fut.set_result(inner)
                # Always store expected filament + duration for live tracking
                fila_g = inner.get("total_filament_used")
                if fila_g is not None and float(fila_g or 0) > 0:
                    self._expected_filament_g = float(fila_g)
                pt = inner.get("print_time")
                if pt is not None and int(pt or 0) > 0:
                    self._expected_print_time_s = int(pt)
                if self._expected_filament_g or self._expected_print_time_s:
                    print(f"[Printer {self.name}] File metadata: "
                          f"{self._expected_filament_g}g / {self._expected_print_time_s}s")
                    print(f"[Printer {self.name}] 1046 full response: {json.dumps(inner)[:600]}")
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
            # error_code is a scalar, not one of _CC2_STATE_KEYS's dict values,
            # so the comprehension above skips it — capture it separately so
            # it isn't silently dropped (previously it was excluded from the
            # "unknown keys" debug warning above but never actually stored
            # anywhere).
            if "error_code" in source:
                self._cc2_state["error_code"] = source["error_code"]
            # machine_model/sn/hostname are scalars from method 1001's response,
            # same "not a dict so the comprehension above skips it" situation
            # as error_code.
            for key in ("machine_model", "sn", "hostname"):
                if key in source:
                    self._cc2_state[key] = source[key]
            if updates:
                deep_merge(self._cc2_state, updates)
                self._apply_cc2_status()
                # This is the 5s status poller's method 1003 response, not the
                # printer's unsolicited api_status push handled below — it still
                # carries fresh print_status, so transitions must be checked
                # here too or a print start/end seen only via polling is missed.
                await self._check_print_transition()
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

    async def fetch_file_info(self, filename: str) -> None:
        """Fetch thumbnail (1045) + metadata (1046) via MQTT and broadcast file_info."""
        if not self._mqtt_registered:
            return
        loop = asyncio.get_running_loop()
        self._pending_thumb_fut = loop.create_future()
        self._pending_meta_fut  = loop.create_future()
        await self.send_cmd(1045, {"storage_media": "local", "filename": filename})
        await self.send_cmd(1046, {"storage_media": "local", "filename": filename})
        try:
            results = await asyncio.wait_for(
                asyncio.gather(self._pending_thumb_fut, self._pending_meta_fut,
                               return_exceptions=True),
                timeout=10,
            )
        except asyncio.TimeoutError:
            results = [None, {}]
        finally:
            self._pending_thumb_fut = None
            self._pending_meta_fut  = None

        thumb_b64 = results[0] if isinstance(results[0], str) else None
        meta      = results[1] if isinstance(results[1], dict) else {}
        await state.broadcast_to_browsers({
            "type":         "file_info",
            "printer_id":   self.id,
            "filename":     filename,
            "thumbnail_b64": thumb_b64,
            "print_time":   meta.get("print_time"),
            "layers":       meta.get("layer"),
            "filament_g":   meta.get("total_filament_used"),
        })

    def _sync_spoolman_locations(self) -> None:
        """On connect, push all tray-linked spool locations to Spoolman."""
        for spool_id in (state.tray_map.get(self.id) or {}).values():
            if spool_id is not None:
                spoolman_set_location(spool_id, self.id)

    def _active_error_codes(self) -> list:
        """Printer fault codes currently reported, in report order.

        Sources: the scalar error_code (seen on real hardware, 704) and
        machine_status.exception_status, the list Elegoo's own SDK reads.
        exception_status is replaced wholesale by each update (it's a list),
        so it drops codes once cleared; exception.exception_code is a dict
        that deep-merges and so keeps stale keys -- it's used only for the
        timestamp of a code that's still active. Command result codes
        (api_response result.error_code, e.g. 1009 busy) are not faults.
        """
        codes = []
        scalar = self._cc2_state.get("error_code")
        ms = self._cc2_state.get("machine_status") or {}
        listed = ms.get("exception_status")
        for c in [scalar] + (listed if isinstance(listed, list) else []):
            if not c or isinstance(c, bool) or is_api_result_code(c) or c in codes:
                continue
            codes.append(c)
        return codes

    def _protocol_reason_hint(self) -> dict | None:
        # Only codes present in printers/error_codes.py have a confirmed
        # category/message; everything else surfaces raw with "unknown"
        # rather than guessing.
        codes = self._active_error_codes()
        if not codes:
            return None
        ms = self._cc2_state.get("machine_status") or {}
        # Prefer the first code we can explain; otherwise show the first raw one.
        error_code = next((c for c in codes if lookup_error_code(c)), codes[0])
        known = lookup_error_code(error_code)
        raw = {"error_code": self._cc2_state.get("error_code"), "sub_status": ms.get("sub_status")}
        if ms.get("exception_status"):
            raw["exception_status"] = ms["exception_status"]
            times = (self._cc2_state.get("exception") or {}).get("exception_code") or {}
            raw["exception_times"] = {str(c): (times.get(str(c)) or {}).get("time") for c in codes if str(c) in times}
        return {
            "code":     error_code,
            "category": known["category"] if known else "unknown",
            "message":  known["message"] if known else "",
            "action":   known.get("action", "") if known else "",
            "raw":      raw,
        }

    async def _file_list_timeout(self) -> None:
        await asyncio.sleep(10)
        if self._awaiting_file_list:
            self._awaiting_file_list = False
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": [],
                "error": "File list request timed out.",
            })

    # UNVERIFIED against real hardware: follows what Elegoo's own elegoo-link
    # SDK (the library ElegooSlicer uses) does -- chunked PUT /upload on port
    # 80, see uploads.put_file_chunked and spooler-cc2-research.md. Verify with
    # a small file before relying on it, as with the CC1 path before it.
    supports_upload = True
    supports_light = True

    async def set_light(self, on: bool) -> bool:
        return await self.send_cmd(CMD_LIGHT, {"LightStatus": {"SecondLight": bool(on)}})

    async def upload_file(self, local_path, remote_name: str, start_after: bool = False) -> bool:
        from uploads import UploadError, forward_timeout, put_file_chunked
        size = local_path.stat().st_size
        loop = asyncio.get_running_loop()
        token = self.access_code or "123456"   # the SDK's default when no code is set

        def _send():
            return put_file_chunked(self.ip, 80, "/upload", local_path, remote_name, token,
                                    timeout=forward_timeout(size))
        try:
            reply = await loop.run_in_executor(None, _send)
        except OSError as e:
            raise UploadError(f"Could not reach the printer: {e}") from e
        print(f"[Printer {self.name}] CC2 upload -> {reply}")
        if start_after and not await self.start_print_file(remote_name):
            raise UploadError("File uploaded, but the printer didn't accept the start command.")
        return True

    async def start_print_file(self, filename: str, print_opts: dict | None = None) -> bool:
        self._current_filename = filename
        opts = print_opts or {}
        return await self.send_cmd(1020, {
            "filename":      filename,
            "storage_media": "local",
            "config": {
                "delay_video":   bool(opts.get("timelapse")),
                "bedlevel_force": bool(opts.get("leveling")),
                "print_layout":  "B" if opts.get("smooth_plate") else "A",
            },
        })

    def _apply_cc2_status(self) -> None:
        s     = self._cc2_state
        ps    = s.get("print_status", {})
        gm    = s.get("gcode_move", {})
        ext   = s.get("extruder", {})
        bed   = s.get("heater_bed", {})
        ztemp = s.get("ztemperature_sensor", {})
        ms    = s.get("machine_status", {})

        # Device attributes (method 1001) -- same self.attrs shape CC1 already
        # populates, so the existing "fw {version}" subtitle in the frontend
        # just works for CC2 too. Verified live against real hardware
        # (2026-10-05): software_version.ota_version is the user-facing
        # firmware version shown on Elegoo's own app; mcu_version/soc_version
        # exist but aren't what "firmware version" means to a user here.
        sw_version = s.get("software_version")
        if s.get("machine_model") or sw_version or s.get("sn") or s.get("hostname"):
            self.attrs = {
                "Model":           s.get("machine_model", self.attrs.get("Model", "")),
                "FirmwareVersion": (sw_version or {}).get("ota_version", self.attrs.get("FirmwareVersion", "")),
                "MainboardID":     s.get("sn", self.attrs.get("MainboardID", "")),
                "Hostname":        s.get("hostname", self.attrs.get("Hostname", "")),
            }
            self._note_firmware()

        print_duration = ps.get("print_duration", 0) or 0
        remaining      = ps.get("remaining_time_sec", 0) or 0
        state_str      = ps.get("state", "")
        filename_from_ps = (ps.get("filename") or ps.get("task_name") or
                            ps.get("file_name") or ps.get("file", {}).get("filename", ""))
        if filename_from_ps:
            self._current_filename = filename_from_ps
        sub_status     = ms.get("sub_status", 0)

        # external_device.camera -- verified against Elegoo's elegoo-link SDK
        # (externalDeviceStatus.cameraConnected) as the printer's own signal
        # for whether a camera module is physically connected right now.
        # Only set when the printer has actually reported this key at least
        # once; stays None (unknown) otherwise rather than defaulting to
        # "disconnected" on a guess.
        ext_device = s.get("external_device")
        if isinstance(ext_device, dict) and "camera" in ext_device:
            self.camera_connected = bool(ext_device["camera"])

        # sub_status numbers verified against Elegoo's own open-source network SDK
        # (github.com/elegooofficial/elegoo-link,
        # src/lan/adapters/elegoo_fdm_cc2/elegoo_fdm_cc2_message_adapter.cpp's
        # machine_status.sub_status switch) -- not guessed. That SDK observes
        # 2801/2802 (homing) and 2901/2902 (auto-leveling) alongside its own
        # top-level status==PRINTING, i.e. during a print's pre-print phase --
        # we don't read that top-level status field ourselves, but placing
        # them in _SUB_STABLE follows the exact same pattern already verified
        # correct for the sibling preheating codes below (which only take
        # effect when our own print_status.state string doesn't already say
        # "printing", i.e. before the print has actually started extruding).
        _SUB_TRANSIENT = {2501: 5, 2503: 7}
        _SUB_STABLE    = {
            1045: 15, 1096: 15, 1405: 15, 1906: 15,  # extruder/bed preheating
            2801: 1,  2802: 1,                       # homing
            2901: 20, 2902: 20,                      # auto-leveling
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
            # Verified live (2026-10-06): while the printer heats up before a
            # print, print_status.state already says "printing" (duration 0,
            # layer 0) with sub_status 1045 (nozzle) / 1405 (bed) -- showing
            # "printing" then is wrong. Before extruding has begun, let the
            # more specific sub_status decide (heating, homing, leveling).
            if state_str == "printing" and not print_duration and sub_status not in _EXTRUDING_SUBS:
                if sub_status in _SUB_STABLE:
                    status_code = _SUB_STABLE[sub_status]
                else:
                    # Nothing has been extruded yet, so whatever the printer is
                    # doing it isn't "printing": show preparing, and log the
                    # sub_status once so it can be given a proper label
                    # (1066 was seen during bed leveling on 2026-10-06).
                    status_code = 15
                    if sub_status not in _logged_sub_status:
                        _logged_sub_status.add(sub_status)
                        print(f"[Printer {self.name}] CC2 sub_status {sub_status} before extrusion "
                              f"isn't mapped -- shown as Preparing.")
        elif sub_status in _SUB_STABLE:
            status_code = _SUB_STABLE[sub_status]
        elif remaining > 0:
            status_code = 3
        else:
            status_code = 0

        # machine_status.status is the printer's top-level mode (codes from
        # Elegoo's elegoo-link SDK, documented in spooler-cc2-research.md).
        # The print_status/sub_status logic above only knows about print
        # phases, so anything outside a print used to show as idle.
        ms_status = ms.get("status")
        if ms_status == 14:                      # emergency stop
            status_code = 14
        elif ms_status == 15:                    # recovering after power loss
            status_code = 12
        elif status_code == 0 and ms_status in _MS_PREPARING:
            status_code = _MS_PREPARING[ms_status]
        elif status_code == 0 and ms_status is not None and ms_status not in _MS_KNOWN_IDLE_OR_HANDLED:
            if ms_status not in _logged_ms_status:
                _logged_ms_status.add(ms_status)
                print(f"[Printer {self.name}] CC2 machine_status.status {ms_status!r} not mapped -- "
                      f"showing idle. Please report this on GitHub.")

        self.phase = _cc2_phase(ms_status, sub_status, status_code)

        if status_code == 0:
            print_duration = 0
            remaining      = 0

        # CC2 doesn't report remaining_time_sec — estimate from file's expected duration
        if remaining == 0 and state_str == "printing" and self._expected_print_time_s > 0:
            remaining = max(0, self._expected_print_time_s - print_duration)

        total    = print_duration + remaining
        progress = min(100, round(print_duration / total * 100)) if total > 0 else 0

        new_print = state_str == "printing" and self._prev_state_str not in ("printing", "paused")
        if new_print:
            self._filament_mm_max        = 0.0
            self._extruder_offset        = 0.0
            self._last_extruder          = 0.0
            self._expected_filament_g    = 0.0
            self._expected_print_time_s  = 0
            self._last_active_filament_mm = 0.0
            fname = self._current_filename
            if fname and fname != self._meta_fetch_filename:
                self._meta_fetch_filename = fname
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self.send_cmd(1046, {"storage_media": "local", "filename": fname})
                    )
                except RuntimeError:
                    pass

        if state_str:
            self._prev_state_str = state_str

        # CC2 MQTT never sends filament_used or reliable gcode_move.extruder.
        # Use time-progress × expected grams (from method 1046) instead.
        if self._expected_filament_g > 0:
            density = self.filament_density or 1.24
            expected_mm = self._expected_filament_g * 10.0 / (math.pi * 0.0875 ** 2 * density)
            just_finished = state_str in ("complete", "standby") and self._prev_state_str in ("printing", "paused")
            if state_str == "printing" and (print_duration + remaining) > 0:
                filament_mm = print_duration / (print_duration + remaining) * expected_mm
                self._last_active_filament_mm = filament_mm
            elif just_finished:
                filament_mm = expected_mm
            else:
                filament_mm = self._last_active_filament_mm
        else:
            # Fallback: legacy live tracking (usually 0 for CC2 but kept for safety)
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

        sf = gm.get("speed_factor")
        speed_factor = sf if sf is not None else 1.0

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
                if active_tray >= 0:
                    loop.run_in_executor(None, spoolman_assign, self.id, spool_id)
                loop.run_in_executor(None, self._update_filament_density)
                self._on_tray_change(spool_id)
                print(f"[Printer {self.name}] Active tray changed → {active_tray}, spool {spool_id}")
