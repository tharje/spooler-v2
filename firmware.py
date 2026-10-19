"""
Which printer firmware versions have been tested with this version of Spooler.

Spooler speaks reverse-engineered protocols, which break when a manufacturer
changes its firmware. printers/tested_firmware.json lists the versions known to
work; the printer card shows a yellow notice for anything else. It is
information only: nothing is ever blocked.
"""

import fnmatch
import json
from pathlib import Path

import persistence

TESTED_FILE = Path(__file__).parent / "printers" / "tested_firmware.json"


def _load() -> dict:
    try:
        data = json.loads(TESTED_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _norm(version) -> str:
    return str(version or "").strip().lower()


def _entry(printer_type: str) -> dict:
    e = _load().get(printer_type)
    return e if isinstance(e, dict) else {}


def is_tested(printer_type: str, version) -> bool | None:
    """True / False, or None when the version isn't known (no verdict)."""
    v = _norm(version)
    if not v:
        return None
    return any(fnmatch.fnmatchcase(v, _norm(p)) for p in _entry(printer_type).get("tested", []))


def note_for(printer_type: str, version) -> str | None:
    v = _norm(version)
    notes = _entry(printer_type).get("notes") or {}
    for pattern, text in notes.items():
        if fnmatch.fnmatchcase(v, _norm(pattern)):
            return text
    return None


# ── what each printer last reported, to notice a firmware change ────────────

def _seen_file():
    return persistence.DATA_DIR / "firmware_seen.json"


def last_seen(printer_id: str) -> str | None:
    try:
        return (json.loads(_seen_file().read_text()) or {}).get(printer_id)
    except Exception:
        return None


def remember(printer_id: str, version: str) -> None:
    try:
        data = json.loads(_seen_file().read_text())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data[printer_id] = version
    persistence._atomic_write(_seen_file(), json.dumps(data))
