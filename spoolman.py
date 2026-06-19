"""
Spoolman integration: filament database and spool deduction.
"""

import asyncio
import json
import time
import urllib.parse
import urllib.request

import os

from push import load_notif_settings, send_push_all
from state import broadcast_to_browsers

SPOOLMAN_URL    = os.getenv("SPOOLMAN_URL", "http://localhost:7912").rstrip("/")
SPOOLMAN_DB_URL = "https://donkie.github.io/SpoolmanDB/filaments.json"


def get_spoolman_url() -> str:
    return SPOOLMAN_URL
SPOOLMAN_DB_TTL  = 3600  # re-fetch at most once per hour

_spoolman_db: list | None = None
_spoolman_db_fetched: float = 0.0


def get_spoolman_db() -> list:
    global _spoolman_db, _spoolman_db_fetched
    if _spoolman_db is not None and time.time() - _spoolman_db_fetched < SPOOLMAN_DB_TTL:
        return _spoolman_db
    try:
        with urllib.request.urlopen(SPOOLMAN_DB_URL, timeout=10) as resp:
            _spoolman_db = json.loads(resp.read())
            _spoolman_db_fetched = time.time()
            print(f"[SpoolmanDB] Loaded {len(_spoolman_db)} filaments")
    except Exception as e:
        print(f"[SpoolmanDB] Fetch failed: {e}")
        if _spoolman_db is None:
            _spoolman_db = []
    return _spoolman_db


def spoolman_assign(printer_id: str, spool_id: int | None) -> None:
    """Assign a spool to a printer in Spoolman (blocking — run in executor).

    Clears the location on any spool currently assigned to this printer, then
    sets location=printer_id on the new spool (if given).
    """
    try:
        base = get_spoolman_url()
        # Find currently assigned spool and clear it
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(printer_id)}"
        with urllib.request.urlopen(url, timeout=3) as resp:
            current = json.loads(resp.read())
        for s in current:
            if spool_id is None or s["id"] != spool_id:
                req = urllib.request.Request(
                    f"{base}/api/v1/spool/{s['id']}",
                    data=json.dumps({"location": ""}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PATCH",
                )
                urllib.request.urlopen(req, timeout=3).close()
        # Assign the new spool
        if spool_id is not None:
            req = urllib.request.Request(
                f"{base}/api/v1/spool/{spool_id}",
                data=json.dumps({"location": printer_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            urllib.request.urlopen(req, timeout=3).close()
            print(f"[Spoolman] Spool {spool_id} → {printer_id}")
    except Exception as e:
        print(f"[Spoolman] Assign skipped ({e})")


def spoolman_deduct(printer_id: str, amount_g: float, loop: asyncio.AbstractEventLoop) -> None:
    """Deduct used filament from the spool assigned to this printer in Spoolman.

    Designed to run in a thread pool executor; `loop` must be the running event
    loop so we can schedule the browser broadcast from this thread.
    """
    try:
        base = get_spoolman_url()
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(printer_id)}"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
        if not data:
            return
        spool = data[0]
        spool_id = spool["id"]
        body = json.dumps({"use_weight": round(amount_g, 1)}).encode()
        req = urllib.request.Request(
            f"{base}/api/v1/spool/{spool_id}/use",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read())
        remaining = result.get("remaining_weight", 0)
        total     = result.get("initial_weight", 0)
        name      = result.get("filament", {}).get("name") or f"Spool {spool_id}"
        print(f"[Spoolman] {amount_g}g deducted from '{name}' → {remaining}g left")

        if remaining == 0:
            msg: dict | None = {"type": "spool_empty", "spool": result, "printer_id": printer_id}
        elif total > 0 and (remaining / total) < 0.1:
            msg = {"type": "spool_low", "spool": result, "printer_id": printer_id}
        else:
            msg = None

        if msg:
            asyncio.run_coroutine_threadsafe(broadcast_to_browsers(msg), loop)

        # Push notification for configurable low-spool threshold
        notif = load_notif_settings()
        spool_low_cfg = notif.get("spool_low", {})
        if spool_low_cfg.get("enabled") and remaining > 0:
            threshold = float(spool_low_cfg.get("threshold", 100))
            if remaining <= threshold:
                send_push_all(
                    f"Spool almost empty — {name}",
                    f"{round(remaining)}g remaining on {printer_id}.",
                )
    except Exception as e:
        print(f"[Spoolman] Deduct skipped ({e})")
