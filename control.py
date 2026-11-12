"""
Advanced printer control: validation and an audit log.

validate_command is a pure function: it returns an error message, or None when
the command may go ahead. Every advanced action passes through it.
"""

import json
import threading
import time

from persistence import DATA_DIR, _atomic_write
from printers.base import classify_display_state

CONTROL_LOG = DATA_DIR / "control_log.json"
MAX_LOG_ENTRIES = 500
_log_lock = threading.Lock()

HEATERS = {"nozzle": "temp_nozzle", "bed": "temp_bed", "chamber": "temp_chamber"}
# Nozzle PID calibration target range (°C)
PID_MIN, PID_MAX = 150, 280
FANS = {"part": "fan_part", "aux": "fan_aux", "chamber": "fan_chamber"}
AXES = ("X", "Y", "Z")
MAX_STEP_MM = {"X": 10.0, "Y": 10.0, "Z": 10.0}

# Print states in which axes must not be moved.
BUSY_STATES = ("printing", "pausing", "paused", "preparing", "stopping")


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def display_state(printer) -> str:
    return classify_display_state(printer.connected, printer._decoded_printinfo().get("Status"),
                                  printer._is_busy_between_prints())


def validate_command(printer, action: str, params: dict, enabled: bool = True) -> str | None:
    if not enabled:
        return "Advanced control is turned off"
    if not printer.connected:
        return "Printer not connected"
    caps = printer.capabilities
    limits = printer.limits

    if action == "set_temperature":
        heater = params.get("heater")
        if heater not in HEATERS or HEATERS[heater] not in caps:
            return "This printer cannot set that temperature"
        target = _num(params.get("target"))
        if target is None:
            return "Invalid temperature"
        top = (limits.get(heater) or {}).get("max")
        if top is None:
            return "No temperature limit known for this printer"
        if not 0 <= target <= top:
            return f"Temperature must be between 0 and {top:g} °C"
        return None

    if action == "set_fan":
        fan = params.get("fan")
        if fan not in FANS or FANS[fan] not in caps:
            return "This printer does not have that fan"
        pct = _num(params.get("percent"))
        if pct is None or not 0 <= pct <= 100:
            return "Fan speed must be between 0 and 100 %"
        if (limits.get(f"fan_{fan}") or {}).get("idle_only") and display_state(printer) in BUSY_STATES:
            return "This fan cannot be changed while printing"
        return None

    if action == "move_axis":
        if "move" not in caps:
            return "This printer cannot be moved from here"
        if display_state(printer) in BUSY_STATES:
            return "Axes cannot be moved while the printer is printing"
        axis = params.get("axis")
        dist = _num(params.get("distance"))
        if axis not in AXES or dist is None or dist == 0:
            return "Invalid move"
        if abs(dist) > MAX_STEP_MM[axis]:
            return f"Steps are limited to {MAX_STEP_MM[axis]:g} mm"
        return None

    if action == "home":
        if "home" not in caps:
            return "This printer cannot be homed from here"
        if display_state(printer) in BUSY_STATES:
            return "The printer cannot be homed while printing"
        axes = params.get("axes", "XYZ")
        if not isinstance(axes, str) or not axes or any(a not in AXES for a in axes):
            return "Invalid axes"
        if set(axes) != set(AXES) and "home_axes" not in caps:
            return "This printer can only home all axes at once"
        return None

    if action == "auto_level":
        if "level" not in caps:
            return "This printer cannot level from here"
        if display_state(printer) in BUSY_STATES:
            return "The printer cannot level while printing"
        return None

    if action == "pid_calibrate":
        if "pid" not in caps:
            return "This printer cannot run PID calibration from here"
        if display_state(printer) in BUSY_STATES:
            return "PID calibration can't run while the printer is printing"
        target = _num(params.get("target"))
        top = min((limits.get("nozzle") or {}).get("max", PID_MAX), PID_MAX)
        if target is None or not PID_MIN <= target <= top:
            return f"PID target must be between {PID_MIN} and {top:g} °C"
        return None

    if action == "shaper_calibrate":
        if "shaper" not in caps:
            return "This printer cannot run the resonance test from here"
        if display_state(printer) in BUSY_STATES:
            return "The resonance test can't run while the printer is printing"
        return None

    if action == "heaters_off":
        return None   # always allowed (when connected)

    return "Unknown command"


def log_command(user: str, printer, action: str, params: dict, result: str) -> None:
    entry = {"time": time.time(), "user": user, "printer": printer.name, "printer_id": printer.id,
             "action": action, "params": params, "result": result}
    with _log_lock:
        try:
            entries = json.loads(CONTROL_LOG.read_text())
        except Exception:
            entries = []
        entries.append(entry)
        _atomic_write(CONTROL_LOG, json.dumps(entries[-MAX_LOG_ENTRIES:], indent=1))


def read_log(printer_ids=None, name_of=None, limit: int = 100) -> list:
    """Newest first. printer_ids limits it to those printers (older entries
    without an id are matched by name through name_of: {id: name})."""
    try:
        entries = json.loads(CONTROL_LOG.read_text())
    except Exception:
        return []
    if printer_ids is not None:
        names = {(name_of or {}).get(pid) for pid in printer_ids}
        entries = [e for e in entries if e.get("printer_id") in printer_ids
                   or ("printer_id" not in e and e.get("printer") in names)]
    return list(reversed(entries))[:limit]
