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
from persistence import FILAMENT_DENSITY, append_history, filament_mm_to_grams, save_printers
from printers.protocol import decode_printinfo
from push import load_notif_settings, send_push_all
from spoolman import get_spool_density, spoolman_deduct, spoolman_deduct_spool

# Raw SDCP-style status codes. Shared by the pure transition classifier below
# and (for PRINTING) by _check_notifications's "was it actively printing"
# checks elsewhere in this file.
#
# 9 (complete) must stay out of ACTIVE: it's a terminal state, and keeping it
# active would make a direct 9 -> printing transition (reprinting without an
# intervening idle poll) look like "already active", so the per-print
# extrusion snapshot reset in _check_print_transition would never fire.
ACTIVE_STATUSES   = {1, 2, 3, 4, 7, 10, 12, 13, 15, 16, 18, 19, 20, 21}
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
# numbers themselves. Codes not listed here (e.g. 11, 17 — never observed,
# never documented) deliberately fall through to "unknown" rather than being
# guessed at.
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
    10: "preparing",   # checking
    12: "printing",    # recovering (resuming after e.g. power loss)
    13: "printing",    # printing (recovery)
    14: "error",
    15: "preparing",   # warming up
    16: "preparing",   # preheating
    18: "preparing",   # warming up
    19: "preparing",   # warming up
    20: "preparing",   # leveling
    21: "preparing",   # warming up
}

_KIND_BY_DISPLAY_STATE = {
    "pausing":   "pause",
    "paused":    "pause",
    "stopping":  "stop",
    "cancelled": "stop",
    "error":     "error",
}

_logged_unknown_display_codes: set = set()


def classify_display_state(connected: bool, status_code, is_homing_between_prints: bool = False) -> str:
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
    if is_homing_between_prints and status_code == 0:
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
        self._last_print_status = None
        self._print_start_time: float | None = None
        self._spool_extrusion: dict  = {}   # spool_id -> mm used this print
        self._extrusion_snapshot: float = 0.0  # TotalExtrusion at last tray swap
        self._current_print_spool: int | None = None
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

    def _is_homing_between_prints(self) -> bool:
        """CC1-specific quirk: CurrentStatus[0] == 9 (machine-level "homing")
        combined with PrintInfo.Status == 0 means idle-but-homing, not plain
        idle. Other protocols never populate CurrentStatus, so this is a
        harmless no-op for them."""
        arr = self.status.get("CurrentStatus")
        return isinstance(arr, list) and bool(arr) and arr[0] == 9

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
                                   self.connected, pi.get("Status"), self._is_homing_between_prints()),
            "state_reason":    self.state_reason,
            "phase":           self.phase,
            "last_seen":       self.last_seen,
            "attrs":           self.attrs,
            "camera_url":      self.camera_url,
            "camera_connected": self.camera_connected,
            "supports_upload": self.supports_upload,
            "upload_unsupported_reason": None if self.supports_upload else self.upload_unsupported_reason,
            "filament_mm":     round(filament_mm, 1),
            "filament_g":      filament_mm_to_grams(filament_mm, self.filament_density),
        }

    def stop(self) -> None:
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
        """Record + immediately deduct filament used by the outgoing spool.

        Deducting at swap time (rather than waiting for print end) means partial
        prints are correctly accounted for if the server restarts or the printer
        disconnects mid-print.  Pass new_spool_id=None as a sentinel at print end
        to flush the last spool's usage.
        """
        current_mm = float(self._decoded_printinfo().get("TotalExtrusion", 0) or 0)
        delta = current_mm - self._extrusion_snapshot
        if self._current_print_spool is not None and delta > 0:
            self._spool_extrusion[self._current_print_spool] = (
                self._spool_extrusion.get(self._current_print_spool, 0) + delta
            )
            g = filament_mm_to_grams(delta, self.filament_density)
            if g > 0:
                try:
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(
                        None, spoolman_deduct_spool,
                        self._current_print_spool, g, self.id, loop,
                    )
                except RuntimeError:
                    pass
        self._extrusion_snapshot = current_mm
        self._current_print_spool = new_spool_id

    async def request_file_list(self) -> bool:
        from printers.protocol import CMD_LIST_FILES
        return await self.send_cmd(CMD_LIST_FILES, {"Url": "/", "IsDir": True})

    async def start_print_file(self, filename: str, print_opts: dict | None = None) -> bool:
        from printers.protocol import CMD_START
        return await self.send_cmd(CMD_START, {"Filename": filename})

    async def upload_file(self, local_path, remote_name: str, start_after: bool = False) -> bool:
        """Send a file already on disk to the printer's storage, optionally
        starting the print. Returns True on success; raises
        uploads.UploadError with a readable message on failure."""
        from uploads import UploadError
        raise UploadError(self.upload_unsupported_reason)

    # ── Internal helpers ───────────────────────────────────────────────────────

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
            if status == 8:
                send_push_all(f"{self.name} — Print cancelled", "Your print was cancelled.")
            else:
                send_push_all(f"{self.name} — Print complete", "Your print is complete.")

        if s.get("nozzle_idle", {}).get("enabled") and is_idle:
            thr = s["nozzle_idle"].get("threshold", 50)
            if nozzle > thr and not ns["nozzle_idle_fired"]:
                send_push_all(f"{self.name} — Nozzle hot", f"Nozzle is {round(nozzle)}°C while idle.")
                ns["nozzle_idle_fired"] = True
            elif nozzle <= thr:
                ns["nozzle_idle_fired"] = False
        elif not is_idle:
            ns["nozzle_idle_fired"] = False

        if s.get("layer", {}).get("enabled") and is_printing:
            target = s["layer"].get("layer", 1)
            if layer >= target and not ns["layer_fired"]:
                send_push_all(f"{self.name} — Layer {target} reached", f"Currently on layer {layer}.")
                ns["layer_fired"] = True
            if layer < target:
                ns["layer_fired"] = False
        if not is_printing:
            ns["layer_fired"] = False

        if s.get("nozzle_printing", {}).get("enabled") and is_printing:
            thr = s["nozzle_printing"].get("threshold", 260)
            if nozzle > thr and not ns["nozzle_hot_fired"]:
                send_push_all(f"{self.name} — Nozzle overheat", f"Nozzle is {round(nozzle)}°C during print.")
                ns["nozzle_hot_fired"] = True
            elif nozzle <= thr:
                ns["nozzle_hot_fired"] = False
        elif not is_printing:
            ns["nozzle_hot_fired"] = False

        ns["last_status"] = status

    async def _broadcast_state(self) -> None:
        # Only mark "seen" while actually connected -- _broadcast_state() is
        # also called right after a disconnect (connected already flipped to
        # False by the caller) to notify browsers of that, which is not a
        # sign of life from the printer and must not refresh the timestamp.
        if self.connected:
            self.last_seen = time.time()
        self._check_notifications()
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

        display_state = classify_display_state(self.connected, cur_status, self._is_homing_between_prints())
        self._update_state_reason(display_state)

        if event == "start":
            self.state_reason = None
            self._current_print_pauses = []
            self._print_start_time = time.time()
            # Initialise per-spool tracking for this print
            self._spool_extrusion = {}
            self._extrusion_snapshot = 0.0
            canvas = self.status.get("canvas_info", {})
            active_tray = canvas.get("active_tray_id", -1)
            self._current_print_spool = (
                (state.tray_map.get(self.id) or {}).get(str(active_tray))
                if active_tray >= 0 else None
            )

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
            if filament_mm > 0 or filename:
                loop = asyncio.get_running_loop()
                density    = await loop.run_in_executor(None, get_spool_density, self.id)
                self.filament_density = density
                filament_g = filament_mm_to_grams(filament_mm, density)
                end_state = "complete" if completed else ("error" if cur_status == 14 else "cancelled")
                entry = {
                    "id":            uuid.uuid4().hex,
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
                }
                await loop.run_in_executor(None, append_history, entry)
                label = {"complete": "Completed", "error": "Error"}.get(end_state, "Cancelled")
                print(f"[History] {label}: {filename} – {filament_mm:.0f}mm / {filament_g}g"
                      f" (density {density} g/cm³)")
                await state.broadcast_to_browsers({"type": "history_entry", "entry": entry})
                if filament_mm > 0:
                    # Flush + deduct the last active spool immediately
                    self._on_tray_change(None)
                    if self._spool_extrusion:
                        # Log per-spool breakdown (deduction already fired in _on_tray_change)
                        for spool_id, mm in self._spool_extrusion.items():
                            g = filament_mm_to_grams(mm, density)
                            print(f"[History] → spool {spool_id}: {mm:.0f}mm / {g}g")
                    else:
                        # No per-tray mapping: fall back to printer-location lookup
                        loop.run_in_executor(
                            None, spoolman_deduct, self.id, filament_g, loop,
                        )

        self._last_print_status = cur_status
