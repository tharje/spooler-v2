"""
Spoolman integration: filament database and spool deduction.
"""

import asyncio
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import state as _state
from persistence import FILAMENT_DENSITY
import notify
from push import load_notif_settings
from state import broadcast_to_browsers


def _printer_location(printer_id: str) -> str:
    """Return the Spoolman location string for a printer — its name, not its UUID."""
    p = _state.printers.get(printer_id)
    return p.name if p else printer_id

SPOOLMAN_DB_URL = "https://donkie.github.io/SpoolmanDB/filaments.json"


def get_spoolman_url() -> str:
    # Read live every call (not a module constant) so a change made in
    # Settings -> Integrations takes effect immediately, no restart needed --
    # every one of this module's Spoolman calls already funnels through here.
    return config.get("spoolman.url")


def spoolman_auth_header() -> dict:
    """Basic-auth header for Spoolman, if configured (e.g. Spoolman sitting
    behind a reverse proxy that requires it) -- empty dict otherwise."""
    user = config.get("spoolman.auth_user")
    if not user:
        return {}
    pw = config.get("spoolman.auth_pass")
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _spoolman_request(url: str, method: str = "GET", body: bytes | None = None) -> urllib.request.Request:
    """Build a Request with the configured basic-auth header attached (a
    no-op header-wise when none is configured) -- every direct Spoolman call
    in this module goes through this so reverse-proxy basic auth, once set,
    covers all of them uniformly, not just the browser-facing UI proxy."""
    headers = spoolman_auth_header()
    if body is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=body, headers=headers, method=method)


def test_spoolman_connection() -> dict:
    """GET /api/v1/health -- verified directly against a real running Spoolman
    instance (not guessed), returns {"status": "healthy"}. Lightest possible
    check that still proves the URL, network path, and basic auth (if any)
    all actually work."""
    url = get_spoolman_url()
    start = time.time()
    try:
        req = _spoolman_request(f"{url}/api/v1/health")
        with urllib.request.urlopen(req, timeout=5):
            pass
        elapsed_ms = round((time.time() - start) * 1000)
        return {"ok": True, "message": f"Connected in {elapsed_ms} ms"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"HTTP {e.code} from {url}"}
    except Exception as e:
        return {"ok": False, "message": f"Could not reach {url}: {e}"}


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


def get_spool_density(printer_id: str) -> float:
    """Return the filament density (g/cm³) for the spool assigned to this printer.

    Falls back to the PLA default if Spoolman is unreachable or no spool is assigned.
    Designed to run in a thread pool executor.
    """
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(loc)}"
        with urllib.request.urlopen(_spoolman_request(url), timeout=3) as resp:
            data = json.loads(resp.read())
        if data:
            density = data[0].get("filament", {}).get("density")
            if density and float(density) > 0:
                return float(density)
    except Exception:
        pass
    return FILAMENT_DENSITY


def spoolman_set_location(spool_id: int, printer_id: str) -> None:
    """Set location on a single spool without touching any other spools."""
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        req = _spoolman_request(
            f"{base}/api/v1/spool/{spool_id}",
            method="PATCH", body=json.dumps({"location": loc}).encode(),
        )
        urllib.request.urlopen(req, timeout=3).close()
        print(f"[Spoolman] Spool {spool_id} location → {loc}")
    except Exception as e:
        print(f"[Spoolman] Set location skipped ({e})")


def spoolman_assign(printer_id: str, spool_id: int | None) -> None:
    """Assign a spool to a printer in Spoolman (blocking — run in executor).

    Clears the location on any spool currently assigned to this printer, then
    sets location=printer_name on the new spool (if given).
    """
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        # Find currently assigned spool and clear it
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(loc)}"
        with urllib.request.urlopen(_spoolman_request(url), timeout=3) as resp:
            current = json.loads(resp.read())
        for s in current:
            if spool_id is None or s["id"] != spool_id:
                req = _spoolman_request(
                    f"{base}/api/v1/spool/{s['id']}",
                    method="PATCH", body=json.dumps({"location": ""}).encode(),
                )
                urllib.request.urlopen(req, timeout=3).close()
        # Assign the new spool
        if spool_id is not None:
            req = _spoolman_request(
                f"{base}/api/v1/spool/{spool_id}",
                method="PATCH", body=json.dumps({"location": loc}).encode(),
            )
            urllib.request.urlopen(req, timeout=3).close()
            print(f"[Spoolman] Spool {spool_id} → {loc}")
    except Exception as e:
        print(f"[Spoolman] Assign skipped ({e})")


def _notify_spool_level(
    result: dict,
    printer_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Broadcast low/empty spool warnings from a thread-pool executor."""
    remaining = result.get("remaining_weight", 0)
    total     = result.get("initial_weight", 0)
    name      = result.get("filament", {}).get("name") or f"Spool {result.get('id', '?')}"

    if remaining <= 0:
        msg: dict | None = {"type": "spool_empty", "spool": result, "printer_id": printer_id}
    elif total > 0 and (remaining / total) < 0.1:
        msg = {"type": "spool_low", "spool": result, "printer_id": printer_id}
    else:
        msg = None
    if msg:
        asyncio.run_coroutine_threadsafe(broadcast_to_browsers(msg), loop)

    notif = load_notif_settings()
    spool_low_cfg = notif.get("spool_low", {})
    if spool_low_cfg.get("enabled") and remaining > 0:
        threshold = float(spool_low_cfg.get("threshold", 100))
        if remaining <= threshold:
            notify.notify(notify.Notification(
                event="spool_low", printer_id=printer_id,
                title=f"Spool almost empty — {name}",
                body=f"{round(remaining)}g remaining on {printer_id}.",
                extra={"spool_id": result.get("id"), "remaining_g": round(remaining)},
            ))


def spoolman_deduct_spool(
    spool_id: int,
    amount_g: float,
    printer_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Deduct filament from a specific spool by ID. Runs in a thread-pool executor."""
    try:
        base = get_spoolman_url()
        body = json.dumps({"use_weight": round(amount_g, 1)}).encode()
        req = _spoolman_request(f"{base}/api/v1/spool/{spool_id}/use", method="PUT", body=body)
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read())
        name = result.get("filament", {}).get("name") or f"Spool {spool_id}"
        remaining = result.get("remaining_weight", 0)
        print(f"[Spoolman] {amount_g}g deducted from '{name}' → {remaining}g left")
        _notify_spool_level(result, printer_id, loop)
    except Exception as e:
        print(f"[Spoolman] Deduct skipped for spool {spool_id} ({e})")


def spoolman_deduct(printer_id: str, amount_g: float, loop: asyncio.AbstractEventLoop) -> None:
    """Deduct filament from the spool assigned to this printer's location in Spoolman.

    Fallback for single-colour prints where no per-tray tracking is available.
    Runs in a thread-pool executor.
    """
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(loc)}"
        with urllib.request.urlopen(_spoolman_request(url), timeout=3) as resp:
            data = json.loads(resp.read())
        if not data:
            return
        spool_id = data[0]["id"]
        spoolman_deduct_spool(spool_id, amount_g, printer_id, loop)
    except Exception as e:
        print(f"[Spoolman] Deduct skipped ({e})")
