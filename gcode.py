"""
G-code metadata: pull slicer-written config/summary comments out of a file
without loading the whole thing.

OrcaSlicer and ElegooSlicer (both Bambu-Studio-derived) write their config
and a print summary as "; key = value" comments, mostly in a block near the
end of the file. The exact key names here are the commonly documented ones
for that slicer family; they have not been verified against a real exported
file from either slicer (no fixture file was available while writing this)
-- keys that don't match silently give a partial result rather than failing,
so it degrades gracefully if a key is named differently than expected here.
"""

import hashlib
import json
import re
from pathlib import Path

from persistence import DATA_DIR, _atomic_write

GCODE_META_FILE = DATA_DIR / "gcode_meta.json"
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 512 * 1024

# "; key = value" or "; key: value", key may contain letters/digits/underscore.
_LINE_RE = re.compile(rb"^\s*;\s*([A-Za-z0-9_ ()\[\]]+?)\s*[=:]\s*(.+?)\s*$")

_KEYS = {
    "filament used [mm]":                   "filament_used_mm",
    "filament used [g]":                    "filament_used_g",
    "total filament used [g]":              "filament_used_g",
    "estimated printing time (normal mode)": "estimated_time",
    "filament_type":                        "filament_type",
    "filament_density":                     "filament_density",
    "filament_colour":                      "filament_colour",
    "printer_model":                        "printer_model",
}


def _decode_lines(blob: bytes):
    for raw_line in blob.split(b"\n"):
        m = _LINE_RE.match(raw_line.strip(b"\r"))
        if m:
            yield m.group(1).decode("utf-8", "replace").strip().lower(), \
                  m.group(2).decode("utf-8", "replace").strip()


def parse_gcode_meta(head: bytes, tail: bytes) -> dict:
    """Pure function: scan the first ~64 kB and last ~512 kB of a g-code file
    for the slicer's own comment lines and return whatever of the known keys
    were found. Multi-material values (semicolon-separated lists, e.g. two
    colours for a dual-material print) are kept as the raw semicolon-joined
    string, not split -- callers that need a single value use the first
    entry. Missing keys are simply absent from the result."""
    found: dict = {}
    for blob in (head, tail):
        for key, value in _decode_lines(blob):
            field = _KEYS.get(key)
            if field and field not in found:
                found[field] = value
    for numeric_field in ("filament_used_mm", "filament_used_g", "filament_density"):
        if numeric_field in found:
            try:
                found[numeric_field] = float(found[numeric_field].split(";")[0])
            except ValueError:
                del found[numeric_field]
    return found


def first_value(meta: dict, field: str) -> str | None:
    """A multi-material field (e.g. "PLA;PETG") reduced to its first entry."""
    value = meta.get(field)
    return value.split(";")[0].strip() if value else None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def meta_for_file(path: Path, filename: str) -> dict:
    """Parse (or reuse a cached parse of) a g-code file's metadata, keyed by
    filename + its sha256 so a re-upload of the same content never re-parses."""
    digest = file_sha256(path)
    cache_key = f"{filename}:{digest}"
    cache = _load_cache()
    if cache_key in cache:
        return cache[cache_key]
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
        if size > HEAD_BYTES:
            f.seek(max(size - TAIL_BYTES, HEAD_BYTES))
        tail = f.read(TAIL_BYTES)
    meta = parse_gcode_meta(head, tail)
    cache[cache_key] = meta
    _save_cache(cache)
    return meta


def _load_cache() -> dict:
    try:
        return json.loads(GCODE_META_FILE.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    _atomic_write(GCODE_META_FILE, json.dumps(cache, indent=1))
