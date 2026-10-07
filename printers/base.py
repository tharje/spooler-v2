"""
Base printer connection: shared state, status tracking, history, and broadcast.

Subclasses (CC1Connection, CC2Connection, …) must implement:
  - connect()   — establish the transport and run the receive loop
  - send_cmd()  — send a CMD_* command to the printer
"""

import asyncio
import time
import uuid

import state
from features import is_enabled
from persistence import (
    FILAMENT_DENSITY, append_history, filament_mm_to_grams, save_printers,
    save_snapshot, update_history_entry,
)
from printers.protocol import decode_printinfo
import notify as notifylib
from push import load_notif_settings
from snapshot import grab_jpeg
import firmware
import ledger
from printaccount import PrintAccounting, clear_active, load_active, save_active
from spoolman import get_spool_density, last_spool_info

# Raw SDCP-style status codes. Shared by the pure transition classifier below
# and (for PRINTING) by _check_notifications's "was it actively printing"
# checks elsewhere in this file.
#
# 9 (complete) must stay out of ACTIVE: it's a terminal state, and keeping it
# active would make a direct 9 -> printing transition (reprinting without an
# intervening idle poll) look like "already active", so the per-print
# extrusion snapshot reset in _check_print_transition would never fire.
# 11 printer check, 17 resonance test and 22 its completion are steps between
# 10 (file check) and 18 (print start) on CC1 and belong to the print start-up.
ACTIVE_STATUSES   = {1, 2, 3, 4, 7, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 21, 22}
PRINTING_STATUSES = {2, 3, 4, 13}
PAUSED_STATUSES   = {5, 6}   # pausing, paused
_END_STATUSES     = {9, 8, 14, 0}


def classify_print_transition(prev_status, cur_status) -> str | None:
    """Pure state-machine step: given the previous and current raw status
    code, return which transition (if any) just happened.

    Returns "start" when printing begins, "end" when a print finishes
    (completed or cancelled/errored), otherwise None. Has no side effects and
    touches no printer/network/disk state, so it's testable on its own —
    the bookkeeping that reacts to each event (resetting per-print extrusion
    tracking, writing a history entry) stays in _check_print_transition.

    ACTIVE and _END_STATUSES are disjoint by construction (0/8/9/14 never
    appear in ACTIVE_STATUSES), so "start" and "end" can never both apply to
    the same transition.

    "start" excludes resuming from a pause (prev in PAUSED_STATUSES) as well
    as already being active — paused(6) -> printing(2) is a resume, not a new
    print, and must not reset per-print extrusion tracking. (This was a real
    bug: PAUSED_STATUSES was never in ACTIVE_STATUSES, so every resume used to
    satisfy "prev not in ACTIVE" and incorrectly fire "start".)
    """
    if cur_status in ACTIVE_STATUSES and prev_status not in ACTIVE_STATUSES | PAUSED_STATUSES:
        return "start"
    if cur_status in _END_STATUSES and prev_status in PRINTING_STATUSES | PAUSED_STATUSES:
        return "end"
    return None


# ── Normalized display state ─────────────────────────────────────────────────
#
# Raw status codes (0-21) are the same numeric space across CC1/CC2/Moonraker/
# Prusa — each protocol's own code maps its named states onto these numbers
# (see each printer module's _STATE_MAP/_STATE_STR). This table is the single
# place that turns a raw code into the small, stable vocabulary every consumer
# (history, UI, future notifications) should use instead of re-interpreting
# numbers themselves. Codes not listed here deliberately fall through to
# "unknown" rather than being guessed at. CC1's own names for 11-26 come from
# Elegoo's elegoo-link SDK (see spooler-cc1-research.md).
_DISPLAY_STATE_BY_CODE = {
    0:  "idle",
    1:  "preparing",   # homing
    2:  "printing",
    3:  "printing",
    4:  "printing",
    5:  "pausing",
    6:  "paused",
    7:  "stopping",
    8:  "cancelled",
    9:  "complete",
    10: "preparing",   # file checking
    11: "preparing",   # printer checking (CC1)
    12: "printing",    # recovering (resuming after e.g. power loss)
    13: "printing",    # printing (recovery)
    14: "error",
    15: "preparing",   # warming up
    16: "preparing",   # preheating
    18: "preparing",   # warming up
    19: "preparing",   # warming up
    20: "preparing",   # leveling
    21: "preparing",   # warming up
    17: "preparing",   # resonance test (CC1)
    22: "preparing",   # resonance test completed (CC1)
    23: "preparing",   # filament auto-feeding (CC1, from the printer's screen)
    24: "preparing",   # filament unloading (CC1)
    25: "preparing",   # filament unloading abnormal (CC1) -- see phase label
    26: "preparing",   # filament unloading paused (CC1) -- see phase label
}

_KIND_BY_DISPLAY_STATE = {
    "pausing":   "pause",
    "paused":    "pause",
    "stopping":  "stop",
    "cancelled": "stop",
    "error":     "error",
}

_logged_unknown_display_codes: set = set()


def classify_display_state(connected: bool, status_code, busy_between_prints: bool = False) -> str:
    """Map (connected, raw status code) to one of: offline, idle, preparing,
    printing, pausing, paused, stopping, complete, cancelled, error, unknown.

    The numeric lookup is deterministic; the only side effect is a one-time
    log line the first time an unrecognised raw code is seen, so it's never
    silently hidden as "idle" (the previous frontend behavior for any code
    outside its hand-maintained table).
    """
    if not connected:
        return "offline"
    if status_code is None:
        return "idle"
    if busy_between_prints and status_code == 0:
        return "preparing"
    state_str = _DISPLAY_STATE_BY_CODE.get(status_code)
    if state_str is not None:
        return state_str
    if status_code not in _logged_unknown_display_codes:
        _logged_unknown_display_codes.add(status_code)
        print(f"[State] Unrecognised raw status code {status_code!r} — showing as 'unknown'. "
              f"Please report this on GitHub (include the printer type) so it can be mapped.")
    return "unknown"


class PrinterConnection:
    # Subclasses that implement upload_file() set this True; the UI disables
    # the upload button for printers where it stays False.
    supports_upload: bool = False
    # Subclasses that can switch the chamber light from Spooler set this True
    # and implement set_light(); the auto-light option is only offered then.
    supports_light: bool = False
    upload_unsupported_reason: str = "File upload is not supported for this printer type."

    def __init__(
        self,
        printer_id: str,
        ip: str,
        name: str,
        mainboard_id: str = "",
        printer_type: str = "cc1",
        access_code: str = "",
    ):
        self.id           = printer_id
        self.ip           = ip
        self.name         = name
        self.mainboard_id = mainboard_id
        self.printer_type = printer_type
        self.access_code  = access_code
        self.connected    = False
        self.status: dict = {}
        self.attrs: dict  = {}
        self.camera_url: str | None = None
        # True/False when the protocol actually reports whether a camera
        # module is physically connected (currently CC2 only, via
        # external_device.camera); None when the protocol doesn't report
        # this at all, so the frontend can tell "known disconnected" apart
        # from "unknown" instead of assuming disconnected by default.
        self.camera_connected: bool | None = None
        self.filament_density: float = FILAMENT_DENSITY
        self._task: asyncio.Task | None = None
        # Light on when a print starts, off when it ends (per-printer option).
        self.auto_light: bool = False
        self._offline_task: asyncio.Task | None = None   # delayed "printer offline" notice
        self._offline_notified = False
        self._last_print_status = None
        self._print_start_time: float | None = None
        # Filament bookkeeping for the print in progress (None between prints);
        # saved to disk so a Spooler restart mid-print neither loses nor repeats it.
        self._acct: PrintAccounting | None = None
        self._acct_saved_at = 0.0
        self._acct_saved_mm = 0.0
        self._notif_state: dict = {
            "last_status":      None,
            "nozzle_idle_fired": False,
            "layer_fired":      False,
            "nozzle_hot_fired": False,
        }
        self.state_reason: dict | None = None
        # Short label for what a "preparing" printer is doing right now
        # ("Leveling", "Homing", ...); "" when the protocol can't say.
        self.phase: str = ""
        self._last_spooler_cmd_at: float | None = None
        self._current_print_pauses: list = []
        # Epoch seconds (not an ISO string) deliberately — the frontend does
        # "how long ago was this" math against it, which an ISO string without
        # a timezone suffix (as time.strftime("%Y-%m-%dT%H:%M:%S") produces
        # elsewhere in this file) would make ambiguous across server/browser
        # timezones. Updated in _broadcast_state(), not here, so it reflects
        # the last time we actually heard from the printer, not construction.
        self.last_seen: float | None = None

    # ── Public interface ───────────────────────────────────────────────────────

    def mark_spooler_command(self) -> None:
        """Call whenever Spooler itself sends a pause/stop command, so a
        pause/stop state observed shortly after can be attributed to
        "spooler" rather than "printer" or "unknown"."""
        self._last_spooler_cmd_at = time.time()

    def _protocol_reason_hint(self) -> dict | None:
        """Override in a subclass to surface protocol-specific reason info
        when available (e.g. Moonraker's print_stats.message, CC2's
        error_code). Return a dict with any of "code", "category", "message",
        "raw" — or None if this protocol doesn't currently surface anything
        for the printer's present state. Must never guess: only return
        fields actually present in the payload, exactly as reported.
        """
        return None

    def _is_busy_between_prints(self) -> bool:
        """CC1 reports a machine-level mode in CurrentStatus alongside the
        print status. When PrintInfo.Status says idle but the machine is doing
        something else (self-check 4, auto-leveling 5, resonance test 6, busy
        7, file check 8, homing 9, filament unload 10, PID 11), the printer is
        busy, not plain idle. Per Elegoo's SDK the first entry is used, or the
        second when the first is 2 (file transfer). Other protocols never
        populate CurrentStatus, so this is a harmless no-op for them."""
        arr = self.status.get("CurrentStatus")
        if not isinstance(arr, list) or not arr:
            return False
        mode = arr[1] if arr[0] == 2 and len(arr) > 1 else arr[0]
        return mode in (4, 5, 6, 7, 8, 9, 10, 11)

    def _decoded_printinfo(self) -> dict:
        """Decode PrintInfo hex keys and normalise CC1 time fields.

        CC1 SDCP firmware exposes elapsed/total time as CurrentTicks/TotalTicks
        (both in seconds) instead of PrintTime/RemainTime.  Normalise here so the
        browser and history recording can use the same field names regardless of
        printer type.
        """
        pi = decode_printinfo(self.status.get("PrintInfo", {}))
        if self.printer_type == "cc1" and not pi.get("PrintTime"):
            ct = pi.get("CurrentTicks") or 0
            tt = pi.get("TotalTicks") or 0
            if ct:
                pi["PrintTime"]  = ct
                pi["RemainTime"] = max(0, tt - ct)
        return pi

    def to_dict(self) -> dict:
        pi = self._decoded_printinfo()
        filament_mm = pi.get("TotalExtrusion", 0) or 0
        # Replace raw PrintInfo (may have hex-encoded SDCP keys) with decoded version
        # so the browser can read plain field names like Filename directly.
        status = {**self.status, "PrintInfo": pi} if "PrintInfo" in self.status else self.status
        return {
            "id":              self.id,
            "ip":              self.ip,
            "name":            self.name,
            "printer_type":    self.printer_type,
            "has_access_code": bool(self.access_code),
            "mainboard_id":    self.mainboard_id,
            "connected":       self.connected,
            "status":          status,
            "state":           classify_display_state(
                                   self.connected, pi.get("Status"), self._is_busy_between_prints()),
            "state_reason":    self.state_reason,
            "phase":           self.phase,
            "last_seen":       self.last_seen,
            "firmware_version": self.firmware_version,
            "firmware_tested":  firmware.is_tested(self.printer_type, self.firmware_version),
            "firmware_note":    firmware.note_for(self.printer_type, self.firmware_version),
            # The server's clock when this was sent, so a browser whose clock
            # differs can compare last_seen against the server's "now".
            "server_time":     time.time(),
            "attrs":           self.attrs,
            "camera_url":      self.camera_url,
            "camera_connected": self.camera_connected,
            "supports_upload": self.supports_upload,
            "supports_light":  self.supports_light,
            "auto_light":      self.auto_light and self.supports_light,
            "upload_unsupported_reason": None if self.supports_upload else self.upload_unsupported_reason,
            "filament_mm":     round(filament_mm, 1),
            "filament_g":      filament_mm_to_grams(filament_mm, self.filament_density),
        }

    def save_accounting_now(self) -> None:
        """Called when Spooler shuts down: write the running print's filament
        figures to disk now, so a restart carries on from here."""
        if self._acct is not None:
            self._observe_extrusion()
            self._save_accounting(force=True)

    def stop(self) -> None:
        if self._offline_task is not None:
            self._offline_task.cancel()
            self._offline_task = None
        if self._task:
            self._task.cancel()

    async def start(self) -> None:
        try:
            while True:
                await self.connect()
                if not self.connected:
                    print(f"[Printer {self.name}] Retrying in 5 s …")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    # ── Subclass contract ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        raise NotImplementedError

    async def send_cmd(self, cmd: int, data: dict) -> bool:
        raise NotImplementedError

    def _update_filament_density(self) -> None:
        self.filament_density = get_spool_density(self.id)

    def _on_tray_change(self, new_spool_id: int | None) -> None:
        """The spool feeding the extruder changed. Everything extruded so far
        belongs to the outgoing spool; the rest goes to the new one. Between
        prints this does nothing (the next print reads the active tray when it
        starts)."""
        if self._acct is None:
            return
        self._observe_extrusion()
        self._acct.switch_spool(new_spool_id)
        self._save_accounting(force=True)

    # ── Per-print filament accounting (T22) ────────────────────────────────────

    def _tray_spool(self):
        """The Spoolman spool linked to the active Canvas/AMS slot, or None."""
        active = (self.status.get("canvas_info") or {}).get("active_tray_id", -1)
        return (state.tray_map.get(self.id) or {}).get(str(active)) if active is not None and active >= 0 else None

    def _has_trays(self) -> bool:
        return bool((self.status.get("canvas_info") or {}).get("canvas_list"))

    def _observe_extrusion(self) -> None:
        if self._acct is None:
            return
        total = self._decoded_printinfo().get("TotalExtrusion", 0) or 0
        if self._acct.observe(total):
            print(f"[Printer {self.name}] Extrusion counter went back (to {float(total):.0f} mm); "
                  f"adding the earlier {self._acct.carry_mm:.0f} mm to this print's total")
        self._save_accounting()

    def _save_accounting(self, force: bool = False) -> None:
        a = self._acct
        if a is None:
            return
        now = time.monotonic()
        if not force and now - self._acct_saved_at < 20 and abs(a.total_mm() - self._acct_saved_mm) < 50:
            return
        self._acct_saved_at, self._acct_saved_mm = now, a.total_mm()
        try:
            save_active(self.id, a)
        except OSError as e:
            print(f"[Printer {self.name}] Could not save print accounting: {e}")

    async def _capture_start_spool(self, acct: PrintAccounting) -> None:
        """Remember which spool Spoolman has at this printer when the print starts,
        so the deduction still has a target if Spoolman is down at the end."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, get_spool_density, self.id)
        info = last_spool_info(self.id)
        if info.get("status") == "found" and self._acct is acct:
            acct.start_spool = info.get("spool_id")
            self._save_accounting(force=True)

    async def _flush_orphaned_accounting(self, saved: PrintAccounting) -> None:
        """Spooler was down when a print ended. Deduct what it had seen so far,
        marked as such; there is no history entry to attach it to."""
        per = saved.finish()
        loop = asyncio.get_running_loop()
        density = await loop.run_in_executor(None, get_spool_density, self.id)
        await self._record_deductions(saved, per, density, note="The print ended while Spooler was not running; "
                                                              "this is the amount seen before it stopped.")
        clear_active(self.id)
        print(f"[Printer {self.name}] A print ended while Spooler was stopped; recorded its last known filament use")

    async def _record_deductions(self, acct: PrintAccounting, per: dict, density: float,
                                 note: str | None = None) -> list:
        """Write what a print used into the ledger (pending), never sending
        anything from here. Returns [{"id": spool, "g": grams}] for the history."""
        if not is_enabled("spoolman"):
            return []
        loop = asyncio.get_running_loop()
        info = last_spool_info(self.id)
        status = info.get("status")
        tray_printer = self._has_trays()
        used = []
        rows = []
        for key, mm in per.items():
            grams = filament_mm_to_grams(mm, density)
            if grams <= 0:
                continue
            spool_id, st, reason = key, "pending", None
            if key is None:
                if tray_printer:
                    st, reason = "discarded", "No spool is linked to the slot that was feeding"
                else:
                    spool_id = info.get("spool_id") if status == "found" else acct.start_spool
                    if spool_id is None and status == "none":
                        st, reason = "discarded", "No spool is assigned to this printer in Spoolman"
            rows.append((spool_id, grams, mm, st, reason))
            if spool_id is not None and st == "pending":
                used.append({"id": spool_id, "g": grams})

        def write():
            for spool_id, grams, mm, st, reason in rows:
                ledger.add(acct.print_id, self.id, spool_id, grams, mm, status=st, reason=reason,
                           location=self.name, note=note)
        await loop.run_in_executor(None, write)
        if any(r[3] == "pending" for r in rows):
            ledger.kick()
        for spool_id, grams, mm, st, reason in rows:
            print(f"[Ledger] {self.name}: {mm:.0f} mm / {grams} g -> "
                  f"{('spool ' + str(spool_id)) if spool_id is not None else 'unknown spool'} ({st})")
        return used

    async def request_file_list(self) -> bool:
        from printers.protocol import CMD_LIST_FILES
        return await self.send_cmd(CMD_LIST_FILES, {"Url": "/", "IsDir": True})

    async def start_print_file(self, filename: str, print_opts: dict | None = None) -> bool:
        from printers.protocol import CMD_START
        return await self.send_cmd(CMD_START, {"Filename": filename})

    async def set_light(self, on: bool) -> bool:
        """Switch the printer's light. Only meaningful where supports_light."""
        return False

    def _light_state(self) -> bool | None:
        ls = self.status.get("LightStatus")
        if isinstance(ls, dict) and "SecondLight" in ls:
            return bool(ls["SecondLight"])
        return None

    async def _auto_light(self, on: bool) -> None:
        """Apply the auto-light option. Never raises: the light is a
        convenience and must not disturb print tracking."""
        if not (self.auto_light and self.supports_light and self.connected):
            return
        if self._light_state() is on:
            return
        try:
            await self.set_light(on)
        except Exception as e:
            print(f"[Printer {self.name}] Auto light {'on' if on else 'off'} failed: {type(e).__name__}")

    async def _finish_print_extras(self, entry_id: str | None) -> None:
        """After a print ends: the end picture first (the light must still be
        on for it), then the light."""
        try:
            if entry_id:
                await self._save_print_picture(entry_id)
        finally:
            await self._auto_light(False)

    async def upload_file(self, local_path, remote_name: str, start_after: bool = False) -> bool:
        """Send a file already on disk to the printer's storage, optionally
        starting the print. Returns True on success; raises
        uploads.UploadError with a readable message on failure."""
        from uploads import UploadError
        raise UploadError(self.upload_unsupported_reason)

    # ── Internal helpers ───────────────────────────────────────────────────────

    # ── Notifications ──────────────────────────────────────────────────────────

    def _emit(self, event: str, title: str, body: str = "", priority: str = "default",
              extra: dict | None = None) -> None:
        """Queue a notification for `event` (see notify.EVENT_SETTING). Cheap
        no-op when the event is off; grabs a camera picture first when the
        event's setting asks for one. Safe to call from sync code inside the
        event loop."""
        if not notifylib.is_event_on(event):
            return
        try:
            asyncio.get_running_loop().create_task(self._emit_async(event, title, body, priority, extra))
        except RuntimeError:
            notifylib.notify(notifylib.Notification(event, self.id, title, body, None, priority,
                                                    self.name, extra or {}))

    async def _camera_picture(self, max_age_s: float = 20.0) -> bytes | None:
        """One JPEG from the camera, or None (no camera, offline, timeout).
        A picture taken in the last few seconds is reused, so the end-of-print
        picture and a notification about the same moment cost one grab."""
        if not self.camera_url or not self.connected:
            return None
        cached = getattr(self, "_picture_cache", None)
        if cached and time.monotonic() - cached[0] < max_age_s:
            return cached[1]
        jpeg = await asyncio.get_running_loop().run_in_executor(None, grab_jpeg, self.camera_url)
        if jpeg:
            self._picture_cache = (time.monotonic(), jpeg)
        return jpeg

    async def _save_print_picture(self, entry_id: str) -> None:
        """Keep a picture of the finished print with its history entry. Runs
        beside, never inside, history writing and filament deduction."""
        try:
            if not is_enabled("print_snapshot") or not is_enabled("camera"):
                return
            jpeg = await self._camera_picture()
            if not jpeg:
                return
            loop = asyncio.get_running_loop()
            if await loop.run_in_executor(None, save_snapshot, entry_id, jpeg) and \
               await loop.run_in_executor(None, update_history_entry, entry_id, {"snapshot": True}):
                await state.broadcast_to_browsers({"type": "history_snapshot", "id": entry_id})
            else:
                from persistence import delete_snapshots
                delete_snapshots([entry_id])   # entry vanished meanwhile
        except Exception as e:
            print(f"[Printer {self.name}] Print picture skipped: {type(e).__name__}")

    async def _emit_async(self, event, title, body, priority, extra) -> None:
        image = None
        if notifylib.wants_image(event):
            image = await self._camera_picture()
        notifylib.notify(notifylib.Notification(event, self.id, title, body, image, priority,
                                                self.name, extra or {}))

    def _reason_text(self, reason: dict) -> str:
        parts = []
        if reason.get("message"):
            parts.append(reason["message"])
        if reason.get("code") not in (None, ""):
            parts.append(f"(code {reason['code']})")
        if reason.get("action"):
            parts.append(reason["action"])
        return " ".join(parts)

    def _notify_reason(self, reason: dict) -> None:
        """A new pause/error reason was just recorded: tell the user, with the
        cause. Pauses started from Spooler itself aren't news."""
        kind, cat = reason["kind"], reason.get("category")
        if kind == "pause" and reason.get("initiated_by") == "spooler":
            return
        text = self._reason_text(reason)
        extra = {"category": cat, "code": reason.get("code"), "initiated_by": reason.get("initiated_by")}
        if cat == "filament_runout" and kind in ("pause", "error"):
            self._emit("filament_runout", f"{self.name} — Filament ran out", text or "Load filament and resume.",
                       priority="high", extra=extra)
        elif kind == "error":
            self._emit("print_error", f"{self.name} — Print error", text or "The printer reported an error.",
                       priority="urgent", extra=extra)
        elif kind == "pause":
            self._emit("print_paused", f"{self.name} — Print paused", text or "The print was paused.",
                       priority="high", extra=extra)

    def _track_connection_for_notifications(self) -> None:
        """Delayed 'printer offline' and 'printer back online' notices."""
        if self.connected:
            task = getattr(self, "_offline_task", None)
            if task is not None:
                task.cancel()
                self._offline_task = None
            if getattr(self, "_offline_notified", False):
                self._offline_notified = False
                self._emit("printer_online", f"{self.name} — Printer online", "The printer is connected again.")
        elif getattr(self, "_offline_task", None) is None and not getattr(self, "_offline_notified", False):
            try:
                self._offline_task = asyncio.get_running_loop().create_task(self._offline_after_delay())
            except RuntimeError:
                pass

    async def _offline_after_delay(self) -> None:
        minutes = notifylib.event_settings("printer_offline").get("minutes", 5)
        try:
            minutes = max(1.0, float(minutes))
        except (TypeError, ValueError):
            minutes = 5.0
        try:
            await asyncio.sleep(minutes * 60)
        except asyncio.CancelledError:
            return
        self._offline_task = None
        if not self.connected:
            self._offline_notified = True
            self._emit("printer_offline", f"{self.name} — Printer offline",
                       f"No contact with the printer for {minutes:g} minutes.", priority="high")

    def _check_notifications(self) -> None:
        if not is_enabled("notifications"):
            return
        s = load_notif_settings()
        if not s:
            return
        ns = self._notif_state
        pi = self._decoded_printinfo()
        status      = pi.get("Status", 0)
        nozzle      = self.status.get("TempOfNozzle") or self.status.get("NozzleTemp") or 0
        layer       = pi.get("CurrentLayer", 0)
        is_idle     = status == 0
        is_printing = status in (3, 6)
        is_done     = status in (9, 8)
        last        = ns["last_status"]

        # CC2 typically goes 3→0 on completion (no 9/8 reported).
        # Fire "finished" on any transition away from an active print state.
        _was_printing = last in (2, 3, 4, 5, 6, 7)
        _print_ended  = (is_done or is_idle) and _was_printing
        if s.get("finished", {}).get("enabled") and _print_ended:
            fname = pi.get("Filename", "")
            if status == 8:
                self._emit("print_cancelled", f"{self.name} — Print cancelled",
                           f"{fname} was cancelled." if fname else "Your print was cancelled.")
            else:
                self._emit("print_complete", f"{self.name} — Print complete",
                           f"{fname} is done." if fname else "Your print is complete.", extra={"filename": fname})

        if s.get("nozzle_idle", {}).get("enabled") and is_idle:
            thr = s["nozzle_idle"].get("threshold", 50)
            if nozzle > thr and not ns["nozzle_idle_fired"]:
                self._emit("nozzle_hot_idle", f"{self.name} — Nozzle hot", f"Nozzle is {round(nozzle)}°C while idle.")
                ns["nozzle_idle_fired"] = True
            elif nozzle <= thr:
                ns["nozzle_idle_fired"] = False
        elif not is_idle:
            ns["nozzle_idle_fired"] = False

        if s.get("layer", {}).get("enabled") and is_printing:
            target = s["layer"].get("layer", 1)
            if layer >= target and not ns["layer_fired"]:
                self._emit("layer_reached", f"{self.name} — Layer {target} reached", f"Currently on layer {layer}.")
                ns["layer_fired"] = True
            if layer < target:
                ns["layer_fired"] = False
        if not is_printing:
            ns["layer_fired"] = False

        if s.get("nozzle_printing", {}).get("enabled") and is_printing:
            thr = s["nozzle_printing"].get("threshold", 260)
            if nozzle > thr and not ns["nozzle_hot_fired"]:
                self._emit("nozzle_overheat", f"{self.name} — Nozzle overheat", f"Nozzle is {round(nozzle)}°C during print.", priority="high")
                ns["nozzle_hot_fired"] = True
            elif nozzle <= thr:
                ns["nozzle_hot_fired"] = False
        elif not is_printing:
            ns["nozzle_hot_fired"] = False

        ns["last_status"] = status

    @property
    def firmware_version(self) -> str | None:
        """The printer's firmware/software version as it reports it, if it does."""
        v = (self.attrs or {}).get("FirmwareVersion")
        return str(v) if v else None

    def _note_firmware(self) -> None:
        """Log (and optionally notify about) the first time a firmware version
        is seen on this printer, and whenever it changes -- the moment things
        typically break. Safe to call on every update."""
        version = self.firmware_version
        if not version or version == getattr(self, "_fw_noted", None):
            return
        self._fw_noted = version
        previous = firmware.last_seen(self.id)
        if previous == version:
            return
        try:
            firmware.remember(self.id, version)
        except OSError:
            pass
        tested = firmware.is_tested(self.printer_type, version)
        verdict = "" if tested else " It has not been tested with this version of Spooler." if tested is False else ""
        if previous is None:
            print(f"[Printer {self.name}] Firmware {version}.{verdict}")
            return
        print(f"[Printer {self.name}] Firmware changed: {previous} -> {version}.{verdict}")
        self._emit("firmware_changed", f"{self.name} — Firmware changed",
                   f"{previous} → {version}.{verdict}", extra={"from": previous, "to": version, "tested": tested})

    def _mark_seen(self) -> None:
        """Record that the printer itself just said something (a message
        received, a poll that succeeded)."""
        self.last_seen = time.time()

    async def _broadcast_state(self) -> None:
        # last_seen is NOT touched here: this runs for browser-side reasons too
        # (a renamed printer, a disconnect notice) that say nothing about the
        # printer. Protocols call _mark_seen() where they actually hear from it.
        self._check_notifications()
        self._track_connection_for_notifications()
        await state.broadcast_to_browsers({
            "type":    "printer_update",
            "printer": self.to_dict(),
        })

    def _finish_current_pause(self) -> None:
        """Close out the most recent still-open pause segment (resume, or the
        print ending while still paused)."""
        for p in reversed(self._current_print_pauses):
            if p["until"] is None:
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
                p["until"] = now_iso
                try:
                    started = time.mktime(time.strptime(p["since"], "%Y-%m-%dT%H:%M:%S"))
                    ended   = time.mktime(time.strptime(now_iso, "%Y-%m-%dT%H:%M:%S"))
                    p["duration_s"] = int(ended - started)
                except Exception:
                    p["duration_s"] = None
                break

    def _update_state_reason(self, display_state: str) -> None:
        """Set/clear self.state_reason as the printer enters or leaves a
        pause/stop/error state. Set once on first entry into a given kind
        (pausing->paused keeps the same reason, doesn't reset "since"),
        cleared only on resuming to "printing" — NOT on returning to "idle",
        since the whole point is that the last reason stays visible until the
        next print actually starts (see _check_print_transition's "start"
        branch for that reset)."""
        kind = _KIND_BY_DISPLAY_STATE.get(display_state)
        was_pause = self.state_reason is not None and self.state_reason["kind"] == "pause"

        if kind is None:
            if display_state == "printing" and was_pause:
                self._finish_current_pause()
                self.state_reason = None
            return

        if self.state_reason is None or self.state_reason["kind"] != kind:
            hint = self._protocol_reason_hint() or {}
            if self._last_spooler_cmd_at is not None and time.time() - self._last_spooler_cmd_at <= 15:
                initiated_by = "spooler"
            elif hint.get("message") or hint.get("code"):
                initiated_by = "printer"
            else:
                initiated_by = "unknown"
            self.state_reason = {
                "kind":         kind,
                "initiated_by": initiated_by,
                "code":         str(hint["code"]) if hint.get("code") not in (None, "") else "",
                "category":     hint.get("category") or "unknown",
                "message":      hint.get("message") or "",
                "action":       hint.get("action") or "",
                "raw":          hint.get("raw") or {},
                "since":        time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._notify_reason(self.state_reason)
            if kind == "pause":
                self._current_print_pauses.append({
                    "since":        self.state_reason["since"],
                    "until":        None,
                    "duration_s":   None,
                    "initiated_by": initiated_by,
                    "category":     self.state_reason["category"],
                })

    async def _check_print_transition(self) -> None:
        pi = self._decoded_printinfo()
        cur_status = pi.get("Status")
        event = classify_print_transition(self._last_print_status, cur_status)

        display_state = classify_display_state(self.connected, cur_status, self._is_busy_between_prints())
        self._update_state_reason(display_state)

        extras_started = False
        first_status = self._last_print_status is None
        now_active = cur_status in ACTIVE_STATUSES or cur_status in PAUSED_STATUSES
        saved = load_active(self.id) if first_status else None

        if first_status and not now_active and saved is not None:
            # Spooler was off while a print ended: don't lose what it used. If the
            # printer still shows that print's final figure, use it rather than
            # the last one we had time to save.
            if cur_status == 9 and pi.get("Filename") == saved.filename:
                saved.observe(pi.get("TotalExtrusion", 0) or 0)
            await self._flush_orphaned_accounting(saved)
        elif first_status and now_active:
            # First thing seen after (re)start is a print in progress: carry on
            # where we left off instead of treating it as a new print.
            if saved is not None and (not saved.filename or not pi.get("Filename")
                                      or saved.filename == pi.get("Filename")):
                self._acct = saved
                self._print_start_time = saved.started_at
                print(f"[Printer {self.name}] Resumed filament accounting for the running print "
                      f"({saved.total_mm():.0f} mm so far)")
            else:
                if saved is not None:
                    # The saved print is a different one: it ended while Spooler was stopped.
                    await self._flush_orphaned_accounting(saved)
                self._acct = PrintAccounting(current_spool=self._tray_spool(), filename=pi.get("Filename", ""),
                                             started_at=None)
                self._print_start_time = None
                self._save_accounting(force=True)
            event = None      # not a new print: no "started" notice, light or history reset

        if event == "start":
            self.state_reason = None
            self._current_print_pauses = []
            self._print_start_time = time.time()
            self._emit("print_started", f"{self.name} — Print started",
                       pi.get("Filename") or "A print has started.")
            asyncio.create_task(self._auto_light(True))
            self._acct = PrintAccounting(current_spool=self._tray_spool(), filename=pi.get("Filename", ""),
                                         started_at=self._print_start_time)
            self._save_accounting(force=True)
            asyncio.create_task(self._capture_start_spool(self._acct))
        elif self._acct is not None and now_active:
            self._observe_extrusion()

        if event == "end":
            filament_mm = pi.get("TotalExtrusion", 0) or 0
            filename    = pi.get("Filename", "")
            print_time  = pi.get("PrintTime", 0) or 0
            completed   = cur_status == 9
            # The print may have ended while still paused (rare — most
            # protocols resume before stopping — but cheap to guard against
            # leaving a pause segment with no "until").
            if self.state_reason is not None and self.state_reason["kind"] == "pause":
                self._finish_current_pause()
            reason = self.state_reason  # kept as-is on the live printer; only copied into history
            # The accounting knows the true total even if the printer's counter
            # was reset during the print; the history should agree with it.
            acct = self._acct or PrintAccounting(current_spool=self._tray_spool())
            acct.observe(filament_mm)
            filament_mm = acct.total_mm()
            if filament_mm > 0 or filename:
                loop = asyncio.get_running_loop()
                density    = await loop.run_in_executor(None, get_spool_density, self.id)
                self.filament_density = density
                filament_g = filament_mm_to_grams(filament_mm, density)
                end_state = "complete" if completed else ("error" if cur_status == 14 else "cancelled")
                # The accounting's id doubles as the history id, so a ledger entry
                # ("<history id>:<spool>") points straight at its print.
                entry = {
                    "id":            acct.print_id,
                    "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "printer_id":   self.id,
                    "printer_name": self.name,
                    "filename":     filename,
                    "filament_mm":  round(filament_mm, 1),
                    "filament_g":   filament_g,
                    "print_time_s": int(print_time),
                    "completed":    completed,
                    "end_state":     end_state,
                    "stop_reason":   reason["category"] if reason else None,
                    "error_code":    reason["code"] if reason and reason["kind"] == "error" and reason["code"] else None,
                    "error_message": reason["message"] if reason and reason["kind"] == "error" and reason["message"] else None,
                    "initiated_by":  reason["initiated_by"] if reason else None,
                    "pauses":        list(self._current_print_pauses),
                    "started_at":    (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._print_start_time))
                                      if self._print_start_time else None),
                }
                info = last_spool_info(self.id)
                if info.get("material"):
                    entry["material"] = info["material"]
                if info.get("vendor"):
                    entry["vendor"] = info["vendor"]
                await loop.run_in_executor(None, append_history, entry)
                label = {"complete": "Completed", "error": "Error"}.get(end_state, "Cancelled")
                print(f"[History] {label}: {filename} – {filament_mm:.0f}mm / {filament_g}g"
                      f" (density {density} g/cm³)")
                await state.broadcast_to_browsers({"type": "history_entry", "entry": entry})
                extras_started = True
                asyncio.create_task(self._finish_print_extras(entry["id"]))
                if filament_mm > 0 or self._acct is not None:
                    per = acct.finish()
                    used = await self._record_deductions(acct, per, density)
                    if used:
                        loop.run_in_executor(None, update_history_entry, entry["id"], {"spools": used})
                    clear_active(self.id)
            self._acct = None
            if not extras_started:
                clear_active(self.id)

        if event == "end" and not extras_started:
            asyncio.create_task(self._finish_print_extras(None))   # no history entry, still switch the light off

        self._last_print_status = cur_status
