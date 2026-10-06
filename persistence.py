"""
File-backed persistence: printers list, print history, filament utilities.
"""

import json
import math
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

DATA_DIR      = Path(os.getenv("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PRINTERS_FILE = DATA_DIR / "printers.json"
HISTORY_FILE  = DATA_DIR / "history.json"
TRAY_MAP_FILE = DATA_DIR / "tray_map.json"
RAW_DIR       = DATA_DIR / "raw"

CHANGELOG_FILE = Path(__file__).parent / "public" / "changelog.json"


def current_version() -> str:
    """Same source the frontend's version badge reads: changelog.json's first entry."""
    try:
        entries = json.loads(CHANGELOG_FILE.read_text())
        return entries[0]["version"]
    except Exception:
        return "unknown"

# Set SPOOLER_DEBUG_RAW=1 to dump every raw printer message to RAW_DIR, one
# newline-delimited JSON file per printer. Used to collect real fixtures from
# a printer owner's hardware for tests/fixtures/ — never enabled by default,
# since messages may contain serial numbers or access codes.
DEBUG_RAW = os.getenv("SPOOLER_DEBUG_RAW", "").lower() in ("1", "true", "yes")
_RAW_LOCK = threading.Lock()


def dump_raw_message(printer_id: str, source: str, raw) -> None:
    """Append one raw printer message to RAW_DIR/<printer_id>.ndjson.

    No-op unless SPOOLER_DEBUG_RAW is set. Best-effort: never raises, so a
    debug-dump failure can't take down the printer connection it's attached to.
    """
    if not DEBUG_RAW:
        return
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        line = json.dumps({"ts": time.time(), "source": source, "raw": text})
        path = RAW_DIR / f"{printer_id}.ndjson"
        with _RAW_LOCK:
            with open(path, "a") as f:
                f.write(line + "\n")
    except Exception:
        pass

FILAMENT_DENSITY    = 1.24   # g/cm³ for 1.75 mm PLA (default)
FILAMENT_RADIUS_CM  = 0.175 / 2  # 1.75 mm → cm

HISTORY_MAX_ENTRIES = 1000

# One lock per data file, so concurrent writers (printer tasks finishing around
# the same time, browser commands triggering saves) serialize instead of
# racing on the same temp file or interleaving a read-modify-write cycle.
# Locks are fixed module-level objects (not a dynamically-keyed dict) since
# the set of files is fixed and known up front — avoids any question of two
# threads momentarily creating two different Lock instances for the same key.
_PRINTERS_LOCK = threading.Lock()
_HISTORY_LOCK  = threading.Lock()
_TRAY_MAP_LOCK = threading.Lock()


def filament_mm_to_grams(mm: float, density: float = FILAMENT_DENSITY) -> float:
    vol_cm3 = math.pi * FILAMENT_RADIUS_CM ** 2 * (mm / 10)
    return round(vol_cm3 * density, 1)


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        if mode is not None:
            os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
    with _PRINTERS_LOCK:
        _atomic_write(PRINTERS_FILE, json.dumps(data, indent=2), mode=0o600)  # contains access_code


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
    with _TRAY_MAP_LOCK:
        _atomic_write(TRAY_MAP_FILE, json.dumps(tray_map, indent=2))


def append_history(entry: dict) -> None:
    with _HISTORY_LOCK:
        history = load_history()
        history.append(entry)
        if len(history) > HISTORY_MAX_ENTRIES:
            history = history[-HISTORY_MAX_ENTRIES:]
        _atomic_write(HISTORY_FILE, json.dumps(history, indent=2))


def migrate_history_ids() -> None:
    """One-time startup migration: backfill a uuid4 "id" on any history entry
    that predates it (added alongside end_state/stop_reason/etc.). Safe to
    call on every startup — a no-op once every entry already has one.

    Deliberately called outside append_history's lock (load_history() here
    does a plain read, the write below takes _HISTORY_LOCK on its own) —
    threading.Lock isn't reentrant, so nesting this inside an already-held
    _HISTORY_LOCK would deadlock.
    """
    history = load_history()
    changed = False
    for entry in history:
        if "id" not in entry:
            entry["id"] = uuid.uuid4().hex
            changed = True
    if changed:
        with _HISTORY_LOCK:
            _atomic_write(HISTORY_FILE, json.dumps(history, indent=2))
