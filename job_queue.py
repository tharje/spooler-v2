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
the UI's "Plate tømt - start neste" button. There is no automatic retry and
no automatic pause/cancel handling here: a failed upload/start just leaves
the job assigned so the button can be pressed again.

Layout: DATA_DIR/queue/<job id>.gcode (the file, "upload" jobs only) and
queue.json (the job list, in the same shape list_jobs() returns).
"""

import asyncio
import json
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
STATUSES = ("queued", "assigned", "printing", "done", "cancelled")
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


def list_jobs() -> list:
    """Queued/assigned jobs first (by priority, then age), finished ones last."""
    jobs = _load()
    rank = {s: i for i, s in enumerate(STATUSES)}
    return sorted(jobs, key=lambda j: (rank.get(j["status"], 99), -j.get("priority", 0), j["added_at"]))


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
        "error": None,
    }
    with _lock:
        jobs = _load()
        jobs.append(job)
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
        "error": None,
    }
    with _lock:
        jobs = _load()
        jobs.append(job)
        _save(jobs)
    return job


def update_job(job_id: str, changes: dict) -> dict:
    """Priority and quantity can be edited any time; a queued job can also be
    cancelled. Nothing else is editable once a job exists (re-add instead)."""
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
                _save(jobs)
                return job
    raise QueueError("Job not found.")


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
    """Called periodically: give each queued job, in priority order, the
    best idle printer that's not already holding a different job -- a
    printer whose linked spool matches the job's material is preferred over
    one that doesn't (or can't be checked). Never uploads or starts
    anything -- the job just becomes visible on the printer's card as ready,
    waiting for confirm_plate_cleared()."""
    if not is_enabled("job_queue"):
        return
    with _lock:
        jobs = _load()
        assigned_printers = {j["assigned_printer_id"] for j in jobs if j["status"] == "assigned"}
        available = [p for p in state.printers.values()
                     if p.id not in assigned_printers and p.connected
                     and p.to_dict().get("state") in ("idle", "complete", "cancelled")]
        if not available:
            return
        queued = sorted((j for j in jobs if j["status"] == "queued"),
                         key=lambda j: (-j["priority"], j["added_at"]))
        changed = False
        for job in queued:
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


async def confirm_plate_cleared(printer_id: str) -> dict:
    """The user has cleared the plate and pressed the button on the card:
    actually upload this printer's assigned job and start it. On success,
    one unit is consumed -- if more are left, the job goes back to "queued"
    for the next time this or another matching printer is free; otherwise
    it's "done". On failure the job stays "assigned" so the button can be
    pressed again."""
    printer = state.printers.get(printer_id)
    if printer is None:
        raise QueueError("Printer not found.")
    with _lock:
        jobs = _load()
        job = next((j for j in jobs if j["status"] == "assigned" and j["assigned_printer_id"] == printer_id), None)
        if job is None:
            raise QueueError("No job is assigned to this printer.")
    try:
        if job.get("source") == "printer_file":
            # Already on this printer's own storage -- just start it, nothing to upload.
            ok = await printer.start_print_file(job["filename"])
            fail_msg = "Could not start the print."
        else:
            if not _model_matches(job["gcode_printer_model"], printer.printer_type):
                raise QueueError("This file's printer model does not look compatible with this printer; refusing to send it.")
            path = QUEUE_DIR / f"{job['id']}.gcode"
            if not path.exists():
                raise QueueError("The queued file is missing.")
            ok = await printer.upload_file(path, job["filename"], start_after=True)
            fail_msg = "Upload succeeded but starting the print failed."
    except UploadError as e:
        raise QueueError(str(e))
    with _lock:
        jobs = _load()
        for j in jobs:
            if j["id"] == job["id"]:
                if not ok:
                    j["error"] = fail_msg
                    break
                j["quantity"] -= 1
                if j["quantity"] > 0:
                    j["status"] = "queued"
                    j["assigned_printer_id"] = None
                else:
                    j["status"] = "done"
                j["error"] = None
                break
        _save(jobs)
    if not ok:
        raise QueueError(fail_msg)
    return job


async def assign_loop() -> None:
    while True:
        await asyncio.sleep(15)
        try:
            assign_ready_jobs()
        except Exception as e:
            print(f"[Queue] assign_ready_jobs failed: {type(e).__name__}: {e}")
