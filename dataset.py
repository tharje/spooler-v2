"""
Picture collection for a possible own failure detector.

While a print is running, Spooler saves a camera picture every so often to a
folder outside Spooler's own data (the container path in DATASET_DIR, mounted
from the host), together with a small note about the print. When the print
ends, how it ended is written into the note, so the pictures can later be
sorted into good and failed prints. Nothing is ever sent anywhere and nothing
is done to the printer. Off unless the feature is switched on and the folder
exists.

Layout: <folder>/<printer name>/<start time>/<hh-mm-ss>.jpg, plus job.json in
each print's folder and pictures.jsonl (one line per picture).
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import state
from features import is_enabled

PRINTING = {2, 3, 4, 13}              # printers.base.PRINTING_STATUSES
DATASET_DIR = Path(os.environ.get("SPOOLER_DATASET_DIR", "/dataset"))
INTERVAL_S = min(600, max(10, int(os.environ.get("SPOOLER_DATASET_INTERVAL_S") or 30)))
_jobs: dict = {}                      # printer id -> {"dir": Path, "n": pictures so far}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._") or "printer"


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _start_job(p, info: dict) -> dict:
    started = time.localtime()
    folder = DATASET_DIR / _slug(p.name) / time.strftime("%Y-%m-%d_%H-%M-%S", started)
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


def save_picture(job: dict, jpeg: bytes, info: dict) -> None:
    name = time.strftime("%H-%M-%S") + ".jpg"
    (job["dir"] / name).write_bytes(jpeg)
    job["n"] += 1
    line = {"file": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "progress": info.get("Progress"), "layer": info.get("CurrentLayer"),
            "total_layers": info.get("TotalLayer")}
    with open(job["dir"] / "pictures.jsonl", "a") as f:
        f.write(json.dumps(line) + "\n")


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
        job = _jobs[p.id] = await loop.run_in_executor(None, _start_job, p, info)
    await loop.run_in_executor(None, save_picture, job, jpeg, info)


async def collect_loop() -> None:
    while True:
        await asyncio.sleep(INTERVAL_S)
        if not (is_enabled("dataset_collection") and is_enabled("camera") and DATASET_DIR.is_dir()):
            continue
        for p in list(state.printers.values()):
            try:
                await collect_printer(p)
            except Exception as e:
                print(f"[Dataset] {p.name}: {type(e).__name__}")
