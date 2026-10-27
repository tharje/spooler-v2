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
        return None

    if action == "auto_level":
        if "level" not in caps:
            return "This printer cannot level from here"
        if display_state(printer) in BUSY_STATES:
            return "The printer cannot level while printing"
        return None

    if action == "heaters_off":
        return None   # always allowed (when connected)

    return "Unknown command"


def log_command(user: str, printer, action: str, params: dict, result: str) -> None:
    entry = {"time": time.time(), "user": user, "printer": printer.name,
             "action": action, "params": params, "result": result}
    with _log_lock:
        try:
            entries = json.loads(CONTROL_LOG.read_text())
        except Exception:
            entries = []
        entries.append(entry)
        _atomic_write(CONTROL_LOG, json.dumps(entries[-MAX_LOG_ENTRIES:], indent=1))
