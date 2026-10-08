"""
Ledger of filament deductions waiting to reach (or already sent to) Spoolman.

Why it exists: a print's filament use used to be sent to Spoolman once, in the
background, and forgotten if that failed (Spoolman down, restarting, deleted
spool ...). Now every deduction is written here FIRST, and a sender keeps
trying until Spoolman has taken it, or a person decides what to do.

Rules that make it safe:
* The entry id is "<print id>:<spool id>", so the same print can never produce
  two entries for the same spool, however often an end-of-print is seen.
* An entry is never sent twice: Spoolman's /use is not idempotent, so before
  sending an entry is marked "sending". If Spooler stops mid-send, or Spoolman
  goes silent after receiving the request, the outcome is unknown; those
  entries stop and wait for a person (Retry / Discard) instead of guessing.
* Failures back off (30 s, 2 min, 10 min, then hourly) instead of hammering.
"""

import json
import threading
import time

import persistence

STATUSES = ("pending", "sending", "sent", "failed", "discarded", "dismissed")
UNSENT = ("pending", "sending", "failed")
BACKOFF_S = (30, 120, 600, 3600)          # then hourly
ATTENTION_AFTER = 3                        # attempts before the menu badge shows up
KEEP_DAYS = 90

_lock = threading.Lock()


def _file():
    return persistence.DATA_DIR / "spoolman_ledger.json"


def _load() -> list:
    try:
        data = json.loads(_file().read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(entries: list) -> None:
    persistence._atomic_write(_file(), json.dumps(entries, indent=2), mode=0o600)


def backoff_s(attempts: int) -> int:
    return BACKOFF_S[min(max(attempts, 1), len(BACKOFF_S)) - 1]


def entry_id(history_id: str, spool_id) -> str:
    return f"{history_id}:{spool_id if spool_id is not None else 'none'}"


def add(history_id: str, printer_id: str, spool_id, grams: float, mm: float, *, status: str = "pending",
        reason: str | None = None, location: str | None = None, note: str | None = None,
        now: float | None = None) -> bool:
    """Record a deduction. False (and nothing changes) if this print already
    has an entry for that spool."""
    now = now if now is not None else time.time()
    eid = entry_id(history_id, spool_id)
    entry = {
        "id": eid, "history_id": history_id, "printer_id": printer_id, "spool_id": spool_id,
        "grams": round(float(grams), 2), "mm": round(float(mm), 1), "created_at": int(now), "status": status,
        "attempts": 0, "last_error": reason if status == "discarded" else None, "sent_at": None,
        "next_try_at": 0 if status == "pending" else None,
        "location": location, "note": note,
    }
    with _lock:
        entries = _load()
        if any(e.get("id") == eid for e in entries):
            return False
        entries.append(entry)
        _save(entries)
    return True


def get(eid: str) -> dict | None:
    return next((dict(e) for e in _load() if e.get("id") == eid), None)


def all_entries() -> list:
    return _load()


def unsent() -> list:
    return [e for e in _load() if e.get("status") in UNSENT]


def summary() -> dict:
    items = unsent()
    attention = [e for e in items if e["status"] == "failed" and (e.get("attempts", 0) >= ATTENTION_AFTER
                                                                   or e.get("next_try_at") is None)]
    return {"unsent": len(items), "attention": len(attention)}


def due(now: float | None = None) -> list:
    now = now if now is not None else time.time()
    return [e for e in _load()
            if e.get("status") in ("pending", "failed") and e.get("next_try_at") is not None
            and e["next_try_at"] <= now]


def _update(eid: str, fn) -> dict | None:
    with _lock:
        entries = _load()
        for e in entries:
            if e.get("id") == eid:
                fn(e)
                _save(entries)
                return dict(e)
    return None


def mark_sending(eid: str) -> dict | None:
    """Claim an entry before sending it. None if it is no longer sendable
    (already sent, discarded, or claimed by someone else)."""
    claimed = {}

    def f(e):
        if e.get("status") in ("pending", "failed"):
            e["status"] = "sending"
            e["attempts"] = e.get("attempts", 0) + 1
            claimed["ok"] = True
    out = _update(eid, f)
    return out if claimed else None


def mark_sent(eid: str, now: float | None = None) -> None:
    def f(e):
        e["status"] = "sent"
        e["sent_at"] = int(now if now is not None else time.time())
        e["last_error"] = None
        e["next_try_at"] = None
    _update(eid, f)


def mark_failed(eid: str, error: str, *, retry: bool = True, min_wait_s: int = 0, now: float | None = None) -> None:
    """The attempt didn't go through. retry=False means "wait for a person"."""
    now = now if now is not None else time.time()

    def f(e):
        e["status"] = "failed"
        e["last_error"] = error
        e["next_try_at"] = int(now + max(backoff_s(e.get("attempts", 1)), min_wait_s)) if retry else None
    _update(eid, f)


def retry_now(eid: str) -> bool:
    done = {}

    def f(e):
        if e.get("status") in ("failed", "pending"):
            e["status"] = "pending"
            e["next_try_at"] = 0
            done["ok"] = True
    _update(eid, f)
    return bool(done)


def discard(eid: str, reason: str) -> bool:
    done = {}

    def f(e):
        if e.get("status") in UNSENT:
            e["status"] = "discarded"
            e["last_error"] = reason
            e["next_try_at"] = None
            done["ok"] = True
    _update(eid, f)
    return bool(done)


def dismiss(eid: str) -> bool:
    """Hide an already-discarded entry from the list (the user has seen it)."""
    done = {}

    def f(e):
        if e.get("status") == "discarded":
            e["status"] = "dismissed"
            done["ok"] = True
    _update(eid, f)
    return bool(done)


def recover_interrupted() -> int:
    """At startup: entries left "sending" were interrupted, so whether Spoolman
    took them is unknown. Stop them for a person to decide (never auto-resend)."""
    n = {"n": 0}
    with _lock:
        entries = _load()
        for e in entries:
            if e.get("status") == "sending":
                e["status"] = "failed"
                e["next_try_at"] = None
                e["last_error"] = ("Spooler stopped while sending this, so it is not known whether Spoolman "
                                   "received it. Check the spool in Spoolman, then Retry or Discard.")
                n["n"] += 1
        if n["n"]:
            _save(entries)
    return n["n"]


def cleanup(days: int = KEEP_DAYS, now: float | None = None) -> int:
    """Forget sent/discarded entries older than `days`."""
    cutoff = (now if now is not None else time.time()) - days * 86400
    with _lock:
        entries = _load()
        kept = [e for e in entries if e.get("status") in UNSENT
                or (e.get("sent_at") or e.get("created_at") or 0) >= cutoff]
        if len(kept) != len(entries):
            _save(kept)
        return len(entries) - len(kept)


# ── sending ──────────────────────────────────────────────────────────────────

def process_due(use_fn, find_spool_fn, on_sent=None, now: float | None = None) -> int:
    """Try every entry that is due, once. Blocking (network); run in an
    executor. `use_fn(spool_id, grams)` -> spoolman.spoolman_use-style dict;
    `find_spool_fn(printer_id)` -> ("found", id) | ("none", None) | ("unreachable", None).
    Returns how many entries changed state."""
    changed = 0
    for e in due(now):
        claimed = mark_sending(e["id"])
        if claimed is None:
            continue
        changed += 1
        spool_id = claimed.get("spool_id")
        if spool_id is None:
            # Spoolman was unreachable when the print ended, so the spool wasn't
            # known; use whatever spool is at this printer's location now.
            outcome, found = find_spool_fn(claimed["printer_id"])
            if outcome == "unreachable":
                mark_failed(claimed["id"], "Could not reach Spoolman", now=now)
                continue
            if outcome == "none":
                mark_failed(claimed["id"], "No spool is assigned to this printer in Spoolman",
                            min_wait_s=3600, now=now)
                continue
            spool_id = found
            _update(claimed["id"], lambda x: x.update(spool_id=spool_id))
        res = use_fn(spool_id, claimed["grams"])
        kind = res.get("outcome")
        if kind == "ok":
            mark_sent(claimed["id"], now)
            if on_sent:
                try:
                    on_sent(claimed, res.get("result") or {})
                except Exception as err:
                    print(f"[Ledger] after-send hook failed: {type(err).__name__}")
        elif kind == "not_found":
            mark_failed(claimed["id"], res.get("message", "Spool not found"), min_wait_s=3600, now=now)
        elif kind == "uncertain":
            mark_failed(claimed["id"], res.get("message", "Unknown whether Spoolman received this"),
                        retry=False, now=now)
        else:
            mark_failed(claimed["id"], res.get("message", "Spoolman did not accept it"), now=now)
    return changed


# ── background sender ────────────────────────────────────────────────────────

_wake_cb = None


def set_wake(callback) -> None:
    """The sender registers how to wake it up (called from any thread)."""
    global _wake_cb
    _wake_cb = callback


def kick() -> None:
    """New or retried work: try soon instead of waiting for the next tick."""
    cb = _wake_cb
    if cb is not None:
        try:
            cb()
        except Exception:
            pass


async def sender_loop(enabled_fn, use_fn, find_spool_fn, on_sent=None, on_change=None,
                      tick_s: float = 30.0) -> None:
    """Forever: send what is due (while `enabled_fn()`), clean old entries now
    and then, and tell `on_change(summary)` after anything moved."""
    import asyncio
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    set_wake(lambda: loop.call_soon_threadsafe(wake.set))
    recovered = await loop.run_in_executor(None, recover_interrupted)
    if recovered:
        print(f"[Ledger] {recovered} deduction(s) were interrupted by a restart and now wait for a decision")
    last_cleanup = 0.0
    while True:
        try:
            if time.time() - last_cleanup > 6 * 3600:
                last_cleanup = time.time()
                removed = await loop.run_in_executor(None, cleanup)
                if removed:
                    print(f"[Ledger] Forgot {removed} old entr{'y' if removed == 1 else 'ies'}")
            if enabled_fn():
                changed = await loop.run_in_executor(
                    None, lambda: process_due(use_fn, find_spool_fn, on_sent))
                if changed and on_change:
                    on_change(summary())
        except Exception as e:
            print(f"[Ledger] sender error: {type(e).__name__}: {e}")
        try:
            await asyncio.wait_for(wake.wait(), tick_s)
        except asyncio.TimeoutError:
            pass
        wake.clear()
