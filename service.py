"""
Service logbook: what was done to each printer and when, plus reminders that
come due after a set number of print hours.

Print hours are a running counter per printer (the history is trimmed, so it
can't be summed); the first time a printer is seen the counter starts from
what the history holds. A reminder is due after a number of print hours or of
days. It is announced once when it comes due and again only after it has been
marked as done.
"""

import json
import threading
import time
import uuid

from persistence import DATA_DIR, _atomic_write, load_history
from printers.error_codes import reference_table
from stats import end_state_of

SERVICE_FILE = DATA_DIR / "service_log.json"
_lock = threading.Lock()

KINDS = ("lubrication", "nozzle", "belt", "cleaning", "calibration", "part_replaced", "other")
MAX_LOG = 2000
MAX_TASKS = 200
MAX_ERROR_HISTORY = 50  # shown on the service page; the full record stays in history.json


class ServiceError(Exception):
    pass


def _load() -> dict:
    try:
        d = json.loads(SERVICE_FILE.read_text())
    except Exception:
        d = {}
    return {"tasks": list(d.get("tasks") or []), "log": list(d.get("log") or []),
            "seconds": dict(d.get("seconds") or {})}


def _save(d: dict) -> None:
    _atomic_write(SERVICE_FILE, json.dumps(d, indent=2))


def _history_seconds(printer_id: str, history: list | None = None) -> int:
    hist = load_history() if history is None else history
    return sum((e.get("print_time_s") or 0) for e in hist if e.get("printer_id") == printer_id)


def print_hours(printer_id: str, history: list | None = None, data: dict | None = None) -> float:
    """Hours this printer has printed: the running counter, or the history until there is one."""
    secs = (data or _load())["seconds"].get(printer_id)
    if secs is None:
        secs = _history_seconds(printer_id, history)
    return round(secs / 3600, 1)


def set_print_hours(printer_id: str, hours) -> float:
    """Set the printer's total print hours (for example to include what it printed before
    Spooler). Reminders keep their countdown: only prints from now on count towards them."""
    if not printer_id:
        raise ServiceError("Choose a printer.")
    try:
        hours = round(float(hours), 1)
    except (TypeError, ValueError):
        raise ServiceError("The hours must be a number.")
    if not 0 <= hours <= 1_000_000:
        raise ServiceError("The hours are out of range.")
    with _lock:
        d = _load()
        delta = hours - print_hours(printer_id, data=d)
        d["seconds"][printer_id] = int(hours * 3600)
        for t in d["tasks"]:
            if t.get("printer_id") == printer_id:
                t["done_at_hours"] = round((t.get("done_at_hours") or 0) + delta, 3)
        _save(d)
    return hours


def add_print_time(printer_id: str, seconds) -> None:
    """Call after a print has been added to the history."""
    try:
        seconds = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        return
    with _lock:
        d = _load()
        if printer_id in d["seconds"]:
            d["seconds"][printer_id] += seconds
        else:
            d["seconds"][printer_id] = _history_seconds(printer_id)      # already includes this print
        _save(d)


def _clean_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _days_since(t: dict, now: float) -> int:
    return int(max(0, now - (t.get("since_ts") or t.get("created_at") or now)) // 86400)


def _task_view(t: dict, hours: float, now: float | None = None) -> dict:
    """One reminder as the page shows it. `unit` is "hours" or "days": what it counts."""
    now = time.time() if now is None else now
    since_h = round(max(0.0, hours - (t.get("done_at_hours") or 0)), 1)
    if t.get("interval_days"):
        unit, interval, since = "days", t["interval_days"], _days_since(t, now)
    else:
        unit, interval, since = "hours", t["interval_hours"], since_h
    return {**{k: t.get(k) for k in ("id", "printer_id", "name", "kind", "interval_hours", "interval_days", "last_done")},
            "unit": unit, "interval": interval, "since": since, "hours_since": since_h,
            "remaining": round(interval - since, 1), "due": since >= interval}


def error_history(history: list, printer_id: str | None = None, limit: int = MAX_ERROR_HISTORY) -> list:
    """Past prints that ended in a printer error, most recent first -- the
    service page's error log. Reads what printers/base.py already saves to
    history.json on a failed print (error_code/error_message); records
    nothing new and tracks nothing the printer itself didn't report."""
    out = [
        {
            "timestamp":     e.get("timestamp"),
            "printer_id":    e.get("printer_id"),
            "printer_name":  e.get("printer_name"),
            "filename":      e.get("filename"),
            "error_code":    e.get("error_code"),
            "error_message": e.get("error_message"),
        }
        for e in history
        if end_state_of(e) == "error" and (printer_id is None or e.get("printer_id") == printer_id)
    ]
    out.sort(key=lambda x: x["timestamp"] or "", reverse=True)
    return out[:limit]


def _printer_info(printers: dict, pid: str) -> dict:
    """printers maps id -> {"name", "type"}, or (older callers, and tests)
    id -> name as a plain string; both are accepted."""
    v = printers.get(pid)
    if isinstance(v, dict):
        return v
    return {"name": v} if v else {}


def overview(printers: dict | None = None) -> dict:
    """Everything the page shows. `printers` maps id -> {"name", "type"}
    (type is "cc1"/"cc2"/etc., used only to pick the right error code
    reference table; missing/unknown types just get no reference list)."""
    d = _load()
    hist = load_history()
    data = d
    printers = printers or {}
    ids = list(printers)
    for x in d["tasks"] + d["log"]:
        if x.get("printer_id") not in ids:
            ids.append(x.get("printer_id"))
    hours = {pid: print_hours(pid, hist, data) for pid in ids}
    types_in_use = {_printer_info(printers, pid).get("type") for pid in ids} - {None}
    return {
        "kinds": list(KINDS),
        "printers": [{"id": pid, "name": _printer_info(printers, pid).get("name") or pid,
                      "type": _printer_info(printers, pid).get("type"),
                      "hours": hours[pid]} for pid in ids],
        "tasks": [_task_view(t, hours.get(t["printer_id"], 0.0)) for t in d["tasks"]],
        "log": sorted(d["log"], key=lambda e: (e.get("date", ""), e.get("created_at", 0)), reverse=True),
        "errors": error_history(hist),
        "error_code_reference": {t: reference_table(t) for t in types_in_use},
    }


def add_task(printer_id: str, name: str, interval, kind: str = "other", unit: str = "hours") -> dict:
    name = _clean_text(name, 80)
    if kind not in KINDS:
        raise ServiceError("Unknown kind of service.")
    if not printer_id or not name:
        raise ServiceError("Choose a printer and give the reminder a name.")
    if unit not in ("hours", "days"):
        raise ServiceError("Count the interval in hours or days.")
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        raise ServiceError(f"The number of {unit} must be a whole number.")
    if not 1 <= interval <= (3650 if unit == "days" else 100000):
        raise ServiceError(f"The number of {unit} is out of range.")
    with _lock:
        d = _load()
        if len(d["tasks"]) >= MAX_TASKS:
            raise ServiceError("Too many reminders.")
        now = time.time()
        t = {"id": uuid.uuid4().hex[:12], "printer_id": printer_id, "name": name, "kind": kind,
             "interval_hours": interval if unit == "hours" else None,
             "interval_days": interval if unit == "days" else None,
             "done_at_hours": print_hours(printer_id, data=d), "since_ts": now, "created_at": now,
             "last_done": None, "notified": False}
        d["tasks"].append(t)
        _save(d)
    return t


def delete_task(task_id: str) -> bool:
    with _lock:
        d = _load()
        n = len(d["tasks"])
        d["tasks"] = [t for t in d["tasks"] if t.get("id") != task_id]
        if len(d["tasks"]) == n:
            return False
        _save(d)
    return True


def add_log(printer_id: str, kind: str, note: str = "", date: str | None = None,
            task_id: str | None = None) -> dict:
    if not printer_id:
        raise ServiceError("Choose a printer.")
    if kind not in KINDS:
        raise ServiceError("Unknown kind of service.")
    date = (date or time.strftime("%Y-%m-%d"))[:10]
    try:
        time.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ServiceError("The date must look like 2026-10-11.")
    note = _clean_text(note, 500)
    with _lock:
        d = _load()
        hours = print_hours(printer_id, data=d)
        if len(d["log"]) >= MAX_LOG:
            raise ServiceError("The logbook is full.")
        entry = {"id": uuid.uuid4().hex[:12], "printer_id": printer_id, "kind": kind, "note": note,
                 "date": date, "hours": hours, "task_id": task_id or None, "created_at": time.time()}
        d["log"].append(entry)
        for t in d["tasks"]:
            if task_id and t.get("id") == task_id:
                t.update(done_at_hours=hours, last_done=date, since_ts=time.time(), notified=False)
        _save(d)
    return entry


def delete_log(entry_id: str) -> bool:
    with _lock:
        d = _load()
        n = len(d["log"])
        d["log"] = [e for e in d["log"] if e.get("id") != entry_id]
        if len(d["log"]) == n:
            return False
        _save(d)
    return True


def take_due(printer_id: str) -> list:
    """Reminders for this printer that have come due and not been announced
    yet; they are marked announced, so each is returned once per round."""
    out = []
    with _lock:
        d = _load()
        hours = print_hours(printer_id, data=d)
        changed = False
        for t in d["tasks"]:
            if t.get("printer_id") != printer_id or t.get("notified"):
                continue
            view = _task_view(t, hours)
            if view["due"]:
                t["notified"] = True
                changed = True
                out.append(view)
        if changed:
            _save(d)
    return out
