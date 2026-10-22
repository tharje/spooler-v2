"""
Picture collection for a possible own failure detector.

While a print is running, Spooler saves a camera picture every so often to a
folder outside Spooler's own data (the container path in DATASET_DIR, mounted
from the host), together with a small note about the print. When the print
ends, how it ended is written into the note, so the pictures can later be
sorted into good and failed prints. Nothing is ever sent anywhere and nothing
is done to the printer. Off unless the feature is switched on and a folder is chosen
(in Docker the folder must be mounted from the host, or it is lost with the container).

Layout: <folder>/<printer name>/<start time>/<hh-mm-ss>.jpg, plus job.json in
each print's folder and pictures.jsonl (one line per picture). A picture you
mark from the printer card ("Looks OK" / "Failing now") is taken at that moment
and carries your label; the first "Failing now" is also written into job.json.
The very first picture of each job, taken the moment the print starts (before
it has put anything on the plate), is labelled "before".
"""

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

import config
import state
from features import is_enabled
from snapshot import grab_jpeg

PRINTING = {2, 3, 4, 13}              # printers.base.PRINTING_STATUSES
LABELS = ("ok", "failed")
_jobs: dict = {}                      # printer id -> {"dir": Path, "n": pictures so far}
_lock = threading.Lock()              # the loop and the label button both write


class DatasetError(Exception):
    pass


def dataset_dir() -> Path | None:
    """The folder the user chose in Integrations (or SPOOLER_DATASET_DIR); None = not set."""
    chosen = (config.get("dataset.dir") or "").strip()
    return Path(chosen) if chosen else None


def interval_s() -> int:
    return min(600, max(10, int(config.get("dataset.interval_s") or 30)))


def status_for(printer_id: str) -> dict | None:
    """What the printer card shows while a print is being collected: {pictures}, else None."""
    job = _jobs.get(printer_id)
    return {"pictures": job["n"]} if job and is_enabled("dataset_collection") else None


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._") or "printer"


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _start_job(p, info: dict) -> dict:
    started = time.localtime()
    folder = dataset_dir() / _slug(p.name) / time.strftime("%Y-%m-%d_%H-%M-%S", started)
    folder.mkdir(parents=True, exist_ok=True)
    job = {"dir": folder, "n": 0,
           "meta": {"printer": p.name, "printer_type": p.printer_type,
                    "file": info.get("Filename") or "", "started": time.strftime("%Y-%m-%dT%H:%M:%S", started),
                    "ended": None, "outcome": None, "label": None}}
    _write_json(folder / "job.json", job["meta"])
    return job


def _end_job(p, job: dict) -> None:
    """The print is no longer running: note how it ended (the printer's own word for it)."""
    job["meta"]["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    job["meta"]["outcome"] = p.to_dict().get("state")
    _write_json(job["dir"] / "job.json", job["meta"])


def save_picture(job: dict, jpeg: bytes, info: dict, label: str | None = None) -> None:
    with _lock:
        name = time.strftime("%H-%M-%S") + (f"_{label}" if label else "") + ".jpg"
        (job["dir"] / name).write_bytes(jpeg)
        job["n"] += 1
        line = {"file": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "progress": info.get("Progress"), "layer": info.get("CurrentLayer"),
                "total_layers": info.get("TotalLayer"), "label": label}
        with open(job["dir"] / "pictures.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
        if label == "failed" and not job["meta"].get("failure_seen_at"):
            job["meta"]["failure_seen_at"] = line["at"]
            _write_json(job["dir"] / "job.json", job["meta"])


def label_now(p, label: str) -> None:
    """Take a picture now and keep it with the label you chose. Runs in a request thread."""
    if label not in LABELS:
        raise DatasetError("The label must be ok or failed.")
    if dataset_dir() is None:
        raise DatasetError("Choose a picture folder under Integrations first.")
    info = p._decoded_printinfo()
    if info.get("Status") not in PRINTING:
        raise DatasetError("The printer is not printing.")
    jpeg = grab_jpeg(p.camera_url) if p.camera_url and p.connected else None
    if not jpeg:
        raise DatasetError("Could not get a picture from the camera.")
    with _lock:
        job = _jobs.get(p.id)
        if job is None:
            job = _jobs[p.id] = _start_job(p, info)
    save_picture(job, jpeg, info, label)


async def capture_before_print(p, info: dict) -> None:
    """Called right when a new print starts: the earliest look we get at the plate,
    before this print has put anything on it. Builds up an "empty/ready plate" class
    alongside the during-print pictures, for a future plate-readiness check."""
    if not (is_enabled("dataset_collection") and is_enabled("camera") and dataset_dir() is not None):
        return
    jpeg = await p._camera_picture(max_age_s=0)
    if not jpeg:
        return
    loop = asyncio.get_running_loop()
    job = _jobs.get(p.id)
    if job is None:
        job = await loop.run_in_executor(None, _start_job, p, info)
        job = _jobs.setdefault(p.id, job)          # the loop or label button may have started it meanwhile
    await loop.run_in_executor(None, save_picture, job, jpeg, info, "before")


async def collect_printer(p) -> None:
    info = p._decoded_printinfo()
    job = _jobs.get(p.id)
    if info.get("Status") not in PRINTING:
        if job is not None:
            _jobs.pop(p.id, None)
            await asyncio.get_running_loop().run_in_executor(None, _end_job, p, job)
        return
    jpeg = await p._camera_picture(max_age_s=0)
    if not jpeg:
        return
    loop = asyncio.get_running_loop()
    if job is None:
        job = await loop.run_in_executor(None, _start_job, p, info)
        job = _jobs.setdefault(p.id, job)          # the label button may have started it meanwhile
    await loop.run_in_executor(None, save_picture, job, jpeg, info)


async def collect_loop() -> None:
    while True:
        await asyncio.sleep(interval_s())
        if not (is_enabled("dataset_collection") and is_enabled("camera") and dataset_dir() is not None):
            continue
        for p in list(state.printers.values()):
            try:
                await collect_printer(p)
            except Exception as e:
                print(f"[Dataset] {p.name}: {type(e).__name__}")
