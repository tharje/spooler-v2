"""
File-backed persistence: printers list, print history, filament utilities.
"""

import json
import math
import os
from pathlib import Path

DATA_DIR      = Path(os.getenv("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PRINTERS_FILE = DATA_DIR / "printers.json"
HISTORY_FILE  = DATA_DIR / "history.json"
TRAY_MAP_FILE = DATA_DIR / "tray_map.json"

FILAMENT_DENSITY    = 1.24   # g/cm³ for 1.75 mm PLA (default)
FILAMENT_RADIUS_CM  = 0.175 / 2  # 1.75 mm → cm

HISTORY_MAX_ENTRIES = 1000


def filament_mm_to_grams(mm: float, density: float = FILAMENT_DENSITY) -> float:
    vol_cm3 = math.pi * FILAMENT_RADIUS_CM ** 2 * (mm / 10)
    return round(vol_cm3 * density, 1)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def save_printers(printers: dict) -> None:
    data = [
        {
            "id":           p.id,
            "ip":           p.ip,
            "name":         p.name,
            "printer_type": p.printer_type,
            "access_code":  p.access_code,
        }
        for p in printers.values()
    ]
    _atomic_write(PRINTERS_FILE, json.dumps(data, indent=2))


def load_printers() -> list:
    if not PRINTERS_FILE.exists():
        return []
    try:
        return json.loads(PRINTERS_FILE.read_text())
    except Exception:
        return []


def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


def load_tray_map() -> dict:
    try:
        return json.loads(TRAY_MAP_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_tray_map(tray_map: dict) -> None:
    _atomic_write(TRAY_MAP_FILE, json.dumps(tray_map, indent=2))


def append_history(entry: dict) -> None:
    history = load_history()
    history.append(entry)
    if len(history) > HISTORY_MAX_ENTRIES:
        history = history[-HISTORY_MAX_ENTRIES:]
    _atomic_write(HISTORY_FILE, json.dumps(history, indent=2))
