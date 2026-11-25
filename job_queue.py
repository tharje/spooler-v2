"""
T20: a simple print queue.

A job is either a newly uploaded g-code file (source "upload") with
requirements (printer type, optionally a specific printer, material,
colour), or a file already sitting on one specific printer's own storage,
picked from its file browser (source "printer_file", add_existing_job()) --
that one is always tied to that printer, since the file only exists there.
Either way a job also has a quantity. assign_ready_jobs() matches queued
jobs to idle printers, preferring a printer whose currently linked spool
(persistence.load_tray_map(), via Spoolman) matches the job's material, but
it never uploads or starts anything by itself -- a job only moves past
"assigned" when confirm_plate_cleared() is called for that printer, which is
the UI's "Plate clear – start" button. There is no automatic retry and
no automatic pause/cancel handling here: a failed upload/start just leaves
the job assigned so the button can be pressed again.

Layout: DATA_DIR/queue/<job id>.gcode (the file, "upload" jobs only) and
queue.json (the job list, in the same shape list_jobs() returns).
"""

import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path

import state
from features import is_enabled
from persistence import DATA_DIR, _atomic_write
from gcode import first_value, meta_for_file
from uploads import UploadError, sanitize_filename

QUEUE_DIR = DATA_DIR / "queue"
QUEUE_FILE = QUEUE_DIR / "queue.json"
STATUSES = ("queued", "assigned", "printing", "done", "error", "stopped", "cancelled")
# A print that ended on the printer: completed, failed, or stopped by someone.
FINISHED = ("done", "error", "stopped", "cancelled")
# How the list is shown: what's on a printer first, then the waiting jobs in
# the order they'll go, then the finished ones.
_LIST_RANK = {"printing": 0, "assigned": 1, "queued": 2}
# A job sent to a printer that hasn't started printing it after this long is given up on.
START_TIMEOUT_S = 10 * 60
_lock = threading.Lock()


class QueueError(Exception):
    pass


def _load() -> list:
    try:
        return json.loads(QUEUE_FILE.read_text())
    except Exception:
        return []


def _save(jobs: list) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(QUEUE_FILE, json.dumps(jobs, indent=1))


def print_info(meta: dict) -> dict:
    """What the start dialog shows for a queued file: time, weight and the filament of each
    tool in the slice ("color_map" as the CC2 reports it for its own files)."""
    colours = [c.strip() for c in (meta.get("filament_colour") or "").split(";")]
    types = [t.strip() for t in (meta.get("filament_type") or "").split(";")]
    tools = [{"t": i, "color": c if re.fullmatch(r"#[0-9A-Fa-f]{6}", c) else "",
              "name": types[i] if i < len(types) else ""}
             for i, c in enumerate(colours) if c or (i < len(types) and types[i])]
    return {"print_time": _seconds(meta.get("estimated_time")),
            "filament_g": meta.get("filament_used_g"),
            "color_map": tools}


def _seconds(text) -> int | None:
    """ "1h 8m 44s" (or "2d 3h 1m 0s") -> seconds, None if there is none."""
    parts = re.findall(r"(\d+)\s*([dhms])", text or "")
    if not parts:
        return None
    unit = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * unit[u] for n, u in parts)


def _queue_order(jobs: list) -> list:
    """The waiting jobs, next one first. The queue's order is the order of the
    records in queue.json; priority only decides where a new job is put in
    (see _insert). The sort is stable, so files from before manual ordering keep
    their old priority-then-age order."""
    return sorted((j for j in jobs if j["status"] == "queued"), key=lambda j: -j.get("priority", 0))


def _insert(jobs: list, job: dict) -> None:
    """Put a new job last among the waiting jobs with the same or higher
    priority, i.e. before the first one with a lower priority."""
    for i, other in enumerate(jobs):
        if other["status"] == "queued" and other.get("priority", 0) < job["priority"]:
            jobs.insert(i, job)
            return
    jobs.append(job)


def list_jobs() -> list:
    """Jobs on a printer first, then the waiting ones in queue order (next one
    first), finished ones last. Uploaded files queued before their print info
    was kept get it now."""
    with _lock:
        jobs = _load()
        missing = [j for j in jobs if j.get("source") == "upload" and "print_info" not in j]
        for j in missing:
            path = QUEUE_DIR / f"{j['id']}.gcode"
            try:
                j["print_info"] = print_info(meta_for_file(path, j["filename"])) if path.exists() else {}
            except Exception:
                j["print_info"] = {}
        if missing:
            _save(jobs)
    queued = {j["id"]: i for i, j in enumerate(_queue_order(jobs))}
    finished = sorted((j for j in jobs if j["status"] not in _LIST_RANK),
                      key=lambda j: j.get("finished_at") or "", reverse=True)    # newest first
    active = sorted((j for j in jobs if j["status"] in _LIST_RANK),
                    key=lambda j: (_LIST_RANK[j["status"]], queued.get(j["id"], 0)))
    return active + finished


def get_job(job_id: str) -> dict | None:
    """A single job record by id, or None."""
    with _lock:
        return next((j for j in _load() if j["id"] == job_id), None)


def _spool_material(printer) -> str | None:
    """Best-effort: the material of whatever spool is linked to this printer's
    active tray right now, or None if Spoolman/tray-link isn't set up or the
    feature is off -- callers treat None as "can't tell", not as a mismatch."""
    if not is_enabled("spoolman"):
        return None
    try:
        spool_id = printer._tray_spool()
        if spool_id is None:
            return None
        from spoolman import get_spool_index
        return (get_spool_index().get(spool_id) or {}).get("material")
    except Exception:
        return None


def add_job(local_path: Path, filename: str, requirements: dict) -> dict:
    """Validate, move the file into the queue folder and append a job record.
    requirements: printer_type, printer_id, material, colour (all optional/None),
    quantity (default 1), priority (default 0, higher = sooner)."""
    name = sanitize_filename(filename)
    quantity = max(1, int(requirements.get("quantity") or 1))
    priority = int(requirements.get("priority") or 0)
    printer_type = (requirements.get("printer_type") or "").strip() or None
    printer_id = (requirements.get("printer_id") or "").strip() or None
    if printer_id and printer_id not in state.printers:
        raise QueueError("No such printer.")
    try:
        meta = meta_for_file(local_path, name)
    except Exception as e:
        raise QueueError(f"Could not read the g-code file: {type(e).__name__}")
    gcode_model = first_value(meta, "printer_model")
    job_id = uuid.uuid4().hex
    dest = QUEUE_DIR / f"{job_id}.gcode"
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    local_path.replace(dest)
    job = {
        "id": job_id,
        "source": "upload",
        "filename": name,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "printer_type": printer_type,
        "printer_id": printer_id,
        "material": (requirements.get("material") or "").strip() or None,
        "colour": (requirements.get("colour") or "").strip() or None,
        "quantity": quantity,
        "priority": priority,
        "status": "queued",
        "assigned_printer_id": None,
        "gcode_printer_model": gcode_model,
        "gcode_filament_type": first_value(meta, "filament_type"),
        "print_info": print_info(meta),
        "owner": (requirements.get("owner") or "").strip() or None,
        "error": None,
    }
    with _lock:
        jobs = _load()
        _insert(jobs, job)
        _save(jobs)
    return job


def add_existing_job(printer_id: str, filename: str, requirements: dict) -> dict:
    """Queue a file that's already on a specific printer's own storage (picked
    from its file browser) to print next, instead of uploading a new one.
    Tied to that one printer -- there's no cross-printer matching to do,
    since the file only exists there. confirm_plate_cleared() starts it with
    start_print_file() rather than uploading anything."""
    printer = state.printers.get(printer_id)
    if printer is None:
        raise QueueError("No such printer.")
    quantity = max(1, int(requirements.get("quantity") or 1))
    priority = int(requirements.get("priority") or 0)
    job = {
        "id": uuid.uuid4().hex,
        "source": "printer_file",
        "filename": filename,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "printer_type": printer.printer_type,
        "printer_id": printer_id,
        "material": (requirements.get("material") or "").strip() or None,
        "colour": (requirements.get("colour") or "").strip() or None,
        "quantity": quantity,
        "priority": priority,
        "status": "queued",
        "assigned_printer_id": None,
        "gcode_printer_model": None,   # already on this exact printer -- nothing to validate
        "gcode_filament_type": None,
        "owner": (requirements.get("owner") or "").strip() or None,
        "error": None,
    }
    with _lock:
        jobs = _load()
        _insert(jobs, job)
        _save(jobs)
    return job


def update_job(job_id: str, changes: dict) -> dict:
    """Priority and quantity can be edited any time; a job still in the queue
    can also be moved one place up or down ({"move": "up"|"down"}, see _move)
    or cancelled. Nothing else is editable once a job exists (re-add instead)."""
    if "move" in changes:
        with _lock:
            jobs = _load()
            job = _move(jobs, job_id, changes["move"])
            _save(jobs)
        assign_ready_jobs()     # a printer whose job was moved away gets the new first one
        return get_job(job["id"]) or job
    with _lock:
        jobs = _load()
        for job in jobs:
            if job["id"] == job_id:
                if job["status"] not in ("queued", "assigned") and changes.get("status") != "cancelled":
                    raise QueueError("That job is no longer in the queue.")
                if "priority" in changes:
                    job["priority"] = int(changes["priority"])
                if "quantity" in changes:
                    job["quantity"] = max(1, int(changes["quantity"]))
                if changes.get("status") == "cancelled":
                    job["status"] = "cancelled"
                    job["assigned_printer_id"] = None
                    job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                _save(jobs)
                return job
    raise QueueError("Job not found.")


def _active_order(jobs: list) -> list:
    """The jobs still in the queue as the list shows them: the ones ready on a
    printer first, then the waiting ones in queue order."""
    return ([j for j in jobs if j["status"] == "assigned"]
            + _queue_order(jobs))


def _move(jobs: list, job_id: str, direction) -> dict:
    """Swap a job with the one before or after it in the list. It takes over its
    neighbour's priority, so a later job added with a priority still lands where
    that priority says. A job that was ready on a printer and is part of the
    swap goes back to waiting, so the printer is given whichever job is now
    first (assign_ready_jobs, run by update_job right after)."""
    if direction not in ("up", "down"):
        raise QueueError("move must be up or down.")
    order = _active_order(jobs)
    i = next((n for n, j in enumerate(order) if j["id"] == job_id), None)
    if i is None:
        if any(j["id"] == job_id for j in jobs):
            raise QueueError("That job is no longer in the queue.")
        raise QueueError("Job not found.")
    k = i - 1 if direction == "up" else i + 1
    if not 0 <= k < len(order):
        return order[i]
    slots = [n for n, j in enumerate(jobs) if j["status"] in ("assigned", "queued")]
    order[i], order[k] = order[k], order[i]
    order[k]["priority"] = order[i].get("priority", 0)
    for j in (order[i], order[k]):
        if j["status"] == "assigned":
            j["status"] = "queued"
            j["assigned_printer_id"] = None
    # write the new order back into the queue's slots in the file, and keep the
    # waiting jobs' priorities falling along it so the file order is the queue order
    for n, j in zip(slots, order):
        jobs[n] = j
    last = None
    for j in jobs:
        if j["status"] == "queued":
            if last is not None and j.get("priority", 0) > last:
                j["priority"] = last
            last = j.get("priority", 0)
    return order[k]


def delete_job(job_id: str) -> None:
    with _lock:
        jobs = _load()
        remaining = [j for j in jobs if j["id"] != job_id]
        if len(remaining) == len(jobs):
            raise QueueError("Job not found.")
        _save(remaining)
    (QUEUE_DIR / f"{job_id}.gcode").unlink(missing_ok=True)


# What the slicer is believed to write into printer_model, per printer_type.
# Not verified against a real exported file from either slicer -- a mismatch
# here degrades to "can't tell" (see _model_matches), it never blocks a job.
_MODEL_HINTS = {
    "cc1": ("centauri carbon",),
    "cc2": ("centauri carbon 2",),
    "prusa": ("prusa",),
    "moonraker": ("klipper", "voron", "ratrig"),  # Moonraker runs many different machines
}


def _model_matches(gcode_printer_model: str | None, printer_type: str) -> bool:
    """True if the model string looks compatible, False if it looks like a
    real mismatch (e.g. "centauri carbon 2" on a cc1), and True (benefit of
    the doubt) if we can't tell -- a missing/unrecognised model never blocks
    a job on its own, since the format isn't verified against real files.
    Some hints are substrings of others (e.g. "centauri carbon" is a prefix
    of "centauri carbon 2"), so the LONGEST matching hint wins, not just any
    match for printer_type's own hints."""
    if not gcode_printer_model:
        return True
    text = gcode_printer_model.lower()
    matches = [(hint, family) for family, hints in _MODEL_HINTS.items() for hint in hints if hint in text]
    if not matches:
        return True
    best_family = max(matches, key=lambda hf: len(hf[0]))[1]
    return best_family == printer_type


def _matches(job: dict, printer) -> bool:
    if job["printer_id"] and job["printer_id"] != printer.id:
        return False
    if job["printer_type"] and job["printer_type"] != printer.printer_type:
        return False
    return _model_matches(job["gcode_printer_model"], printer.printer_type)


def _best_printer_for(job: dict, available: list) -> object | None:
    candidates = [p for p in available if _matches(job, p)]
    if not candidates:
        return None
    material = (job["material"] or "").lower()
    exact = [p for p in candidates if material and (_spool_material(p) or "").lower() == material]
    return (exact or candidates)[0]


def assign_ready_jobs() -> None:
    """Called periodically: give each queued job, in queue order, the
    best idle printer that's not already holding a different job -- a
    printer whose linked spool matches the job's material is preferred over
    one that doesn't (or can't be checked). Never uploads or starts
    anything -- the job just becomes visible on the printer's card as ready,
    waiting for confirm_plate_cleared()."""
    if not is_enabled("job_queue"):
        return
    with _lock:
        jobs = _load()
        # a printer that was just sent a job may not report printing yet
        assigned_printers = {j["assigned_printer_id"] for j in jobs
                             if j["status"] == "assigned" or (j["status"] == "printing" and not j.get("print_seen"))}
        available = [p for p in state.printers.values()
                     if p.id not in assigned_printers and p.connected
                     and p.to_dict().get("state") in ("idle", "complete", "cancelled")]
        if not available:
            return
        changed = False
        for job in _queue_order(jobs):
            printer = _best_printer_for(job, available)
            if printer is not None:
                job["status"] = "assigned"
                job["assigned_printer_id"] = printer.id
                available.remove(printer)
                changed = True
                if not available:
                    break
        if changed:
            _save(jobs)


def assigned_job_for(printer_id: str) -> dict | None:
    for job in _load():
        if job["status"] == "assigned" and job["assigned_printer_id"] == printer_id:
            return job
    return None


PRINT_OPTS = ("leveling", "timelapse", "smooth_plate")


def clean_print_opts(raw) -> dict | None:
    """The start dialog's choices, as start_print_file() takes them: the yes/no options and
    the filament slot per tool (slot_map, checked against the printer by the printer)."""
    if not isinstance(raw, dict):
        return None
    opts = {k: bool(raw.get(k)) for k in PRINT_OPTS}
    if isinstance(raw.get("slot_map"), list):
        opts["slot_map"] = raw["slot_map"]
    return opts


async def confirm_plate_cleared(printer_id: str, print_opts: dict | None = None,
                                job_id: str | None = None) -> dict:
    """The user has cleared the plate and pressed the button on the card:
    actually upload this printer's assigned job and start it, with the options picked in
    the start dialog (filament slots, leveling, ...; None: the printer's defaults). On success,
    the job is "printing" until the printer says the print is over (print_ended).
    If more copies are left, the one started gets its own "printing" entry and the
    job goes back to "queued" for the next time this or another matching printer is free. On failure the job stays "assigned" so the button can be
    pressed again."""
    printer = state.printers.get(printer_id)
    if printer is None:
        raise QueueError("Printer not found.")
    with _lock:
        jobs = _load()
        job = next((j for j in jobs if j["status"] == "assigned" and j["assigned_printer_id"] == printer_id), None)
        if job is None:
            raise QueueError("No job is assigned to this printer.")
        if job_id and job["id"] != job_id:
            # the queue was reordered after the start dialog was opened
            raise QueueError("The queue has changed: this printer now has a different job. Check it and press start again.")
    # Attribute the print to whoever queued it, for the history entry.
    printer._pending_owner = job.get("owner")
    try:
        if job.get("source") == "printer_file":
            # Already on this printer's own storage -- just start it, nothing to upload.
            ok = await printer.start_print_file(job["filename"], print_opts=print_opts)
            fail_msg = "Could not start the print."
        else:
            if not _model_matches(job["gcode_printer_model"], printer.printer_type):
                raise QueueError("This file's printer model does not look compatible with this printer; refusing to send it.")
            path = QUEUE_DIR / f"{job['id']}.gcode"
            if not path.exists():
                raise QueueError("The queued file is missing.")
            if print_opts is None:
                ok = await printer.upload_file(path, job["filename"], start_after=True)
            else:
                ok = await printer.upload_file(path, job["filename"])
                ok = ok and await printer.start_print_file(job["filename"], print_opts=print_opts)
            fail_msg = "Upload succeeded but starting the print failed."
    except UploadError as e:
        raise QueueError(str(e))
    with _lock:
        jobs = _load()
        for n, j in enumerate(jobs):
            if j["id"] == job["id"]:
                if not ok:
                    j["error"] = fail_msg
                    break
                j["error"] = None
                if j["quantity"] > 1:
                    # more copies to go: the one now on the printer gets its own entry
                    j["quantity"] -= 1
                    j["status"] = "queued"
                    j["assigned_printer_id"] = None
                    run = _started(dict(j, id=uuid.uuid4().hex, quantity=1, run_of=j["id"]), printer_id)
                    jobs.insert(n, run)
                else:
                    _started(j, printer_id)
                break
        _save(jobs)
    if not ok:
        raise QueueError(fail_msg)
    return job


def _started(job: dict, printer_id: str) -> dict:
    job.update(status="printing", assigned_printer_id=printer_id, print_seen=False,
               started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), sent_ts=time.time())
    return job


def _printing_job(jobs: list, printer_id: str) -> dict | None:
    return next((j for j in jobs if j["status"] == "printing" and j["assigned_printer_id"] == printer_id), None)


def print_started(printer_id: str) -> None:
    """The printer reports a new print running: the job sent to it is really printing."""
    with _lock:
        jobs = _load()
        job = _printing_job(jobs, printer_id)
        if job is not None and not job.get("print_seen"):
            job["print_seen"] = True
            _save(jobs)


def print_ended(printer_id: str, end_state: str, message: str | None = None) -> None:
    """The print on this printer is over ("complete", "error" or "cancelled", as in the
    history): the queue job that was printing there is done, failed or stopped."""
    with _lock:
        jobs = _load()
        job = _printing_job(jobs, printer_id)
        if job is None:
            return
        job["status"] = {"complete": "done", "error": "error"}.get(end_state, "stopped")
        job["error"] = message if job["status"] == "error" else None
        job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save(jobs)


def expire_unstarted(now: float | None = None) -> None:
    """A job sent to a printer that never began printing it isn't left "printing" forever."""
    now = time.time() if now is None else now
    with _lock:
        jobs = _load()
        changed = False
        for j in jobs:
            if (j["status"] == "printing" and not j.get("print_seen")
                    and now - j.get("sent_ts", now) > START_TIMEOUT_S):
                j["status"] = "error"
                j["error"] = "The printer never started this print."
                j["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                changed = True
        if changed:
            _save(jobs)


async def assign_loop() -> None:
    while True:
        await asyncio.sleep(15)
        try:
            expire_unstarted()
            assign_ready_jobs()
        except Exception as e:
            print(f"[Queue] assign_ready_jobs failed: {type(e).__name__}: {e}")
