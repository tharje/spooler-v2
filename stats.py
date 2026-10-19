"""
Print statistics computed from the history file. Pure functions: no I/O, no
clock, no network, so the numbers can be checked against a hand count.

History entries come in two generations. Current ones have `end_state`
("complete" | "cancelled" | "error"), `stop_reason`, `material`, ...; older
ones only have `completed` (bool). Both are handled; an old entry that wasn't
completed counts as "cancelled" because it can't be told apart from an error.
"""

import csv
import io
from datetime import date, datetime, timedelta

UNKNOWN_MATERIAL = "Unknown"


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _parse_day(value, end_of_day=False):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        value = value.isoformat()
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if end_of_day and len(str(value)) <= 10:     # a bare date means "through that day"
        d = d.replace(hour=23, minute=59, second=59)
    return d


def end_state_of(entry: dict) -> str:
    st = entry.get("end_state")
    if st in ("complete", "cancelled", "error"):
        return st
    return "complete" if entry.get("completed") else "cancelled"


def filter_entries(history: list, from_ts=None, to_ts=None, printer_id=None) -> list:
    """Entries ending within [from_ts, to_ts] (inclusive; bare dates cover the
    whole day) for one printer if given, oldest first. Entries without a
    readable timestamp are left out."""
    lo, hi = _parse_day(from_ts), _parse_day(to_ts, end_of_day=True)
    out = []
    for e in history:
        t = _parse_ts(e.get("timestamp"))
        if t is None:
            continue
        if lo and t < lo or hi and t > hi:
            continue
        if printer_id and e.get("printer_id") != printer_id:
            continue
        out.append((t, e))
    out.sort(key=lambda p: p[0])
    return [e for _, e in out]


def _bucket_kind(lo: datetime, hi: datetime) -> str:
    days = (hi - lo).days + 1
    return "day" if days <= 45 else ("week" if days <= 200 else "month")


def _bucket_key(t: datetime, kind: str) -> str:
    if kind == "day":
        return t.date().isoformat()
    if kind == "week":
        return (t.date() - timedelta(days=t.weekday())).isoformat()    # Monday
    return f"{t.year:04d}-{t.month:02d}"


def _next_bucket(d: date, kind: str) -> date:
    if kind == "day":
        return d + timedelta(days=1)
    if kind == "week":
        return d + timedelta(days=7)
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _grams(e: dict) -> float:
    try:
        return float(e.get("filament_g") or 0)
    except (TypeError, ValueError):
        return 0.0


def _seconds(e: dict) -> int:
    try:
        return max(0, int(e.get("print_time_s") or 0))
    except (TypeError, ValueError):
        return 0


def _material_grams(e: dict) -> dict:
    """Grams per material for one entry: per-spool figures when the print used
    several spools (those carry their own material only if known; otherwise the
    entry's material), else the whole print under the entry's material."""
    material = e.get("material") or UNKNOWN_MATERIAL
    return {material: _grams(e)}


def compute_stats(history: list, from_ts=None, to_ts=None, printer_id=None) -> dict:
    entries = filter_entries(history, from_ts, to_ts, printer_id)
    times = [_parse_ts(e["timestamp"]) for e in entries]

    result = {"complete": 0, "cancelled": 0, "error": 0}
    by_material: dict = {}
    by_printer: dict = {}
    reasons: dict = {}
    total_g = total_s = 0.0
    done_s = done_n = 0

    for e in entries:
        st = end_state_of(e)
        result[st] += 1
        g, s = _grams(e), _seconds(e)
        total_g += g
        total_s += s
        if st == "complete":
            done_s += s
            done_n += 1
        for mat, grams in _material_grams(e).items():
            by_material[mat] = by_material.get(mat, 0.0) + grams
        name = e.get("printer_name") or "Unknown"
        p = by_printer.setdefault(name, {"prints": 0, "hours": 0.0, "grams": 0.0})
        p["prints"] += 1
        p["hours"] += s / 3600
        p["grams"] += g
        if st != "complete":
            reason = e.get("error_message") or e.get("stop_reason") or ("error" if st == "error" else "cancelled")
            if st == "error" and e.get("error_code"):
                reason = f"{reason} ({e['error_code']})"
            if reason in ("unknown", None, ""):
                reason = "Cause not known"
            reasons[reason] = reasons.get(reason, 0) + 1

    lo = _parse_day(from_ts) or (times[0] if times else None)
    hi = _parse_day(to_ts, end_of_day=True) or (times[-1] if times else None)
    series = []
    if lo and hi and lo <= hi:
        kind = _bucket_kind(lo, hi)
        buckets: dict = {}
        for t, e in zip(times, entries):
            b = buckets.setdefault(_bucket_key(t, kind), {"prints": 0, "grams": 0.0, "hours": 0.0})
            b["prints"] += 1
            b["grams"] += _grams(e)
            b["hours"] += _seconds(e) / 3600
        d = date.fromisoformat(_bucket_key(lo, kind) + ("-01" if kind == "month" else ""))
        while d <= hi.date():
            key = d.isoformat()[:7] if kind == "month" else d.isoformat()
            b = buckets.get(key, {"prints": 0, "grams": 0.0, "hours": 0.0})
            series.append({"key": key, "prints": b["prints"], "grams": round(b["grams"], 1),
                           "hours": round(b["hours"], 2)})
            d = _next_bucket(d, kind)
    else:
        kind = "day"

    n = len(entries)
    return {
        "prints": n,
        "results": result,
        "success_rate": round(100 * result["complete"] / n, 1) if n else None,
        "hours": round(total_s / 3600, 2),
        "grams": round(total_g, 1),
        "avg_print_s": round(done_s / done_n) if done_n else None,   # completed prints only
        "by_material": {k: round(v, 1) for k, v in sorted(by_material.items(), key=lambda kv: -kv[1])},
        "by_printer": {k: {"prints": v["prints"], "hours": round(v["hours"], 2), "grams": round(v["grams"], 1)}
                       for k, v in sorted(by_printer.items(), key=lambda kv: -kv[1]["prints"])},
        "failure_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "bucket": kind,
        "series": series,
        "from": lo.date().isoformat() if lo else None,
        "to": hi.date().isoformat() if hi else None,
    }


MAX_REFERENCE_LEN = 40


def clean_reference(value) -> str:
    """A reference number/label the user puts on a print: plain text, trimmed,
    no control characters, at most MAX_REFERENCE_LEN characters. Empty clears it."""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError("The reference must be text or a number.")
    text = "".join(c for c in str(value) if c.isprintable()).strip()
    if len(text) > MAX_REFERENCE_LEN:
        raise ValueError(f"The reference can be at most {MAX_REFERENCE_LEN} characters.")
    return text


def _cell(value) -> str:
    """Stop spreadsheet formula injection: a text cell that starts with
    = + - @ (or a tab/CR) gets a leading apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


CSV_COLUMNS = ["Date", "Started", "Reference", "Printer", "File", "Result", "Cause", "Print time (s)", "Filament (m)",
               "Filament (g)", "Material", "Vendor"]


def history_csv(history: list, from_ts=None, to_ts=None, printer_id=None) -> bytes:
    """Semicolon-separated, UTF-8 with a byte-order mark: opens correctly in
    Norwegian-locale Excel (which expects ; and a BOM for æøå)."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for e in filter_entries(history, from_ts, to_ts, printer_id):
        cause = e.get("error_message") or e.get("stop_reason") or ""
        if e.get("error_code"):
            cause = f"{cause} ({e['error_code']})".strip()
        w.writerow([
            e.get("timestamp", ""), e.get("started_at") or "", _cell(e.get("reference", "")), _cell(e.get("printer_name", "")),
            _cell(e.get("filename", "")), end_state_of(e), _cell(cause), _seconds(e),
            str(round((e.get("filament_mm") or 0) / 1000, 2)).replace(".", ","),
            str(round(_grams(e), 1)).replace(".", ","),
            _cell(e.get("material", "")), _cell(e.get("vendor", "")),
        ])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


# ── shape and queries for the external API ───────────────────────────────────

# Plain-English texts for the codes the app stores, so another program doesn't
# have to translate them (the raw codes are always included too).
RESULT_LABELS = {"complete": "Finished", "cancelled": "Stopped", "error": "Failed"}
CATEGORY_LABELS = {
    "filament_runout": "Filament runout", "nozzle_clog": "Nozzle clog", "thermal": "Thermal issue",
    "collision": "Collision detected", "power_loss": "Power loss", "door_open": "Door open",
    "leveling": "Leveling failed", "fan": "Fan fault", "motion": "Motion fault", "sensor": "Sensor fault",
    "hardware": "Hardware fault", "connection": "Lost communication with a printer part",
    "system": "System error", "filament_feed": "Filament feed problem", "filament_tangle": "Filament tangled",
    "user": "Stopped by the user", "unknown": "Cause not known",
}
INITIATOR_LABELS = {"spooler": "Spooler", "printer": "The printer itself", "unknown": "Printer screen, app or unknown"}


def _label(table: dict, value):
    return table.get(value) if value else None


def public_entry(e: dict, base: str = "/api/external/v1", spool_index: dict | None = None,
                 include_raw: bool = False) -> dict:
    """One print as another program should see it: stable names, readable
    texts next to the raw codes, and (with include_raw) the stored entry as is."""
    state = end_state_of(e)
    has_picture = bool(e.get("snapshot"))
    cause = None
    if state != "complete":
        category = e.get("stop_reason")
        text = e.get("error_message") or _label(CATEGORY_LABELS, category) or CATEGORY_LABELS["unknown"]
        if e.get("error_code"):
            text = f"{text} (code {e['error_code']})"
        cause = {
            "text": text,
            "message": e.get("error_message"),
            "category": category,
            "category_label": _label(CATEGORY_LABELS, category),
            "code": e.get("error_code"),
            "initiated_by": e.get("initiated_by"),
            "initiated_by_label": _label(INITIATOR_LABELS, e.get("initiated_by")),
        }
    spools = []
    for sp in e.get("spools") or []:
        info = (spool_index or {}).get(sp.get("id")) or {}
        spools.append({"id": sp.get("id"), "g": sp.get("g"), "name": info.get("name"), "material": info.get("material"),
                       "vendor": info.get("vendor"), "color_hex": info.get("color_hex")})
    pauses = []
    for p in e.get("pauses") or []:
        pauses.append({
            "since": p.get("since"), "until": p.get("until"), "duration_s": p.get("duration_s"),
            "category": p.get("category"), "category_label": _label(CATEGORY_LABELS, p.get("category")),
            "initiated_by": p.get("initiated_by"), "initiated_by_label": _label(INITIATOR_LABELS, p.get("initiated_by")),
        })
    out = {
        "id": e.get("id"),
        "ended_at": e.get("timestamp"),
        "started_at": e.get("started_at"),
        "printer_id": e.get("printer_id"),
        "printer_name": e.get("printer_name"),
        "file": e.get("filename"),
        "result": state,                       # complete | cancelled | error
        "result_label": RESULT_LABELS[state],
        "cause": cause,
        "print_time_s": _seconds(e),
        "filament_mm": round(float(e.get("filament_mm") or 0), 1),
        "filament_m": round((e.get("filament_mm") or 0) / 1000, 3),
        "filament_g": round(_grams(e), 1),
        "material": e.get("material"),
        "vendor": e.get("vendor"),
        "spools": spools,
        "pauses": pauses,
        "reference": e.get("reference") or "",
        "has_picture": has_picture,
        "picture_url": f"{base}/history/{e.get('id')}/picture" if has_picture and e.get("id") else None,
    }
    if include_raw:
        out["raw"] = dict(e)
    return out


def query_history(history: list, from_ts=None, to_ts=None, printer_id=None, text=None,
                  reference=None, result=None, ended_after=None, oldest_first=False) -> list:
    """Newest first unless oldest_first. `text` matches file name or reference
    (case-insensitive); `reference` is an exact match; `result` is complete |
    cancelled | error; `ended_after` keeps prints that ended strictly later than
    that timestamp (for "what's new since my last sync")."""
    rows = filter_entries(history, from_ts, to_ts, printer_id)
    if ended_after:
        cutoff = _parse_ts(ended_after) or _parse_day(ended_after)
        if cutoff is None:
            raise ValueError("ended_after must be a date or a date and time")
        rows = [e for e in rows if _parse_ts(e.get("timestamp")) > cutoff]
    if reference not in (None, ""):
        rows = [e for e in rows if (e.get("reference") or "") == reference]
    if text:
        q = text.lower()
        rows = [e for e in rows if q in f"{e.get('filename') or ''} {e.get('reference') or ''}".lower()]
    if result:
        rows = [e for e in rows if end_state_of(e) == result]
    if not oldest_first:
        rows.reverse()
    return rows
