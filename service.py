"""
Service logbook: what was done to each printer and when, plus reminders that
come due after a set number of print hours.

Print hours are counted from the print history, so they follow the printer
without any extra bookkeeping. A reminder is announced once when it comes due
and again only after it has been marked as done.
"""

import json
import threading
import time
import uuid

from persistence import DATA_DIR, _atomic_write, load_history

SERVICE_FILE = DATA_DIR / "service_log.json"
_lock = threading.Lock()

KINDS = ("lubrication", "nozzle", "belt", "cleaning", "calibration", "part_replaced", "other")
MAX_LOG = 2000
MAX_TASKS = 200


class ServiceError(Exception):
    pass


def _load() -> dict:
    try:
        d = json.loads(SERVICE_FILE.read_text())
    except Exception:
        d = {}
    return {"tasks": list(d.get("tasks") or []), "log": list(d.get("log") or [])}


def _save(d: dict) -> None:
    _atomic_write(SERVICE_FILE, json.dumps(d, indent=2))


def print_hours(printer_id: str, history: list | None = None) -> float:
    """Hours this printer has printed, from the history."""
    hist = load_history() if history is None else history
    secs = sum((e.get("print_time_s") or 0) for e in hist if e.get("printer_id") == printer_id)
    return round(secs / 3600, 1)


def _clean_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _task_view(t: dict, hours: float) -> dict:
    since = round(max(0.0, hours - (t.get("done_at_hours") or 0)), 1)
    interval = t["interval_hours"]
    return {**{k: t.get(k) for k in ("id", "printer_id", "name", "kind", "interval_hours", "last_done")},
            "hours_since": since, "remaining": round(interval - since, 1), "due": since >= interval}


def overview(printers: dict | None = None) -> dict:
    """Everything the page shows. `printers` maps id -> name."""
    d = _load()
    hist = load_history()
    ids = list(printers or {})
    for x in d["tasks"] + d["log"]:
        if x.get("printer_id") not in ids:
            ids.append(x.get("printer_id"))
    hours = {pid: print_hours(pid, hist) for pid in ids}
    return {
        "kinds": list(KINDS),
        "printers": [{"id": pid, "name": (printers or {}).get(pid) or pid, "hours": hours[pid]} for pid in ids],
        "tasks": [_task_view(t, hours.get(t["printer_id"], 0.0)) for t in d["tasks"]],
        "log": sorted(d["log"], key=lambda e: (e.get("date", ""), e.get("created_at", 0)), reverse=True),
    }


def add_task(printer_id: str, name: str, interval_hours, kind: str = "other") -> dict:
    name = _clean_text(name, 80)
    if kind not in KINDS:
        raise ServiceError("Unknown kind of service.")
    if not printer_id or not name:
        raise ServiceError("Choose a printer and give the reminder a name.")
    try:
        interval = int(interval_hours)
    except (TypeError, ValueError):
        raise ServiceError("Hours between services must be a whole number.")
    if not 1 <= interval <= 100000:
        raise ServiceError("Hours between services must be between 1 and 100000.")
    with _lock:
        d = _load()
        if len(d["tasks"]) >= MAX_TASKS:
            raise ServiceError("Too many reminders.")
        t = {"id": uuid.uuid4().hex[:12], "printer_id": printer_id, "name": name, "kind": kind,
             "interval_hours": interval, "done_at_hours": print_hours(printer_id),
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
    hours = print_hours(printer_id)
    with _lock:
        d = _load()
        if len(d["log"]) >= MAX_LOG:
            raise ServiceError("The logbook is full.")
        entry = {"id": uuid.uuid4().hex[:12], "printer_id": printer_id, "kind": kind, "note": note,
                 "date": date, "hours": hours, "task_id": task_id or None, "created_at": time.time()}
        d["log"].append(entry)
        for t in d["tasks"]:
            if task_id and t.get("id") == task_id:
                t.update(done_at_hours=hours, last_done=date, notified=False)
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
        hours = print_hours(printer_id)
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
