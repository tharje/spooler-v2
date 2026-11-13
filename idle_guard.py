"""
Idle guard: heaters left on while a printer is not printing are switched off
after a set number of minutes (Settings key control.idle_heater_min, 0 = off).

Only plain idle states count. Printing, pausing, preheating, leveling, PID and
the resonance test all show as busy, so the timer starts over when one of them
ends. The guard only runs while advanced control is turned on.
"""

import asyncio
import time

import config
import control
import features
import state

CHECK_INTERVAL_S = 20
# Display states in which a heater target means someone left it on.
IDLE_STATES = ("idle", "complete", "cancelled")
GUARD_USER = "Idle guard"

# printer_id -> time.time() when the heaters were first seen on while idle
_since: dict = {}


def minutes() -> int:
    try:
        return max(0, int(config.get("control.idle_heater_min") or 0))
    except (TypeError, ValueError):
        return 0


def _heating(printer) -> bool:
    # Targets sit at the top of printer.status (next to PrintInfo, not inside it).
    status = printer.status or {}
    return any((status.get(k) or 0) > 0 for k in ("TempTargetNozzle", "TempTargetHotbed"))


def _guarded(printer) -> bool:
    return (printer.connected and any(cap in printer.capabilities for cap in control.HEATERS.values())
            and control.display_state(printer) in IDLE_STATES and _heating(printer))


def seconds_left(printer_id: str, now: float | None = None):
    """Seconds until the guard switches this printer's heaters off, or None."""
    limit = minutes()
    start = _since.get(printer_id)
    if not limit or start is None:
        return None
    return max(0, int(start + limit * 60 - (time.time() if now is None else now)))


async def check_once(now: float | None = None) -> list:
    """One pass over all printers. Returns the ids whose heaters were switched off."""
    now = time.time() if now is None else now
    limit = minutes()
    if not limit or not features.is_enabled("advanced_control"):
        _since.clear()
        return []
    switched = []
    for pid, printer in list(state.printers.items()):
        try:
            if not _guarded(printer):
                _since.pop(pid, None)
                continue
            start = _since.setdefault(pid, now)
            if now - start < limit * 60:
                continue
            heaters = [h for h, cap in control.HEATERS.items() if cap in printer.capabilities]
            ok = await printer.heaters_off(heaters)
            control.log_command(GUARD_USER, printer, "heaters_off", {"idle_min": limit},
                                "ok" if ok else "Not supported by this printer yet")
            print(f"[Idle guard] {printer.name}: heaters on for {limit} min without printing, "
                  f"{'switched off' if ok else 'could not switch off'}")
            # Start over either way, so a printer that can't do it isn't retried every pass.
            _since.pop(pid, None)
            if ok:
                switched.append(pid)
        except Exception as e:
            print(f"[Idle guard] {getattr(printer, 'name', pid)}: {type(e).__name__}: {e}")
    return switched


async def watch_loop() -> None:
    while True:
        await asyncio.sleep(CHECK_INTERVAL_S)
        await check_once()
