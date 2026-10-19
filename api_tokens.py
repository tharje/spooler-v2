"""
API keys for other programs that talk to Spooler's external API
(/api/external/v1/...). They are separate from the browser login: a key can
only reach that API, never the rest of Spooler, and is stored only as a hash,
so a copy of the data folder doesn't reveal working keys. The key itself is
shown exactly once, when it is created.
"""

import hashlib
import hmac
import json
import secrets
import threading
import time

import persistence
from persistence import _atomic_write

SCOPES = ("read", "write")        # write = read + change reference numbers
MAX_TOKENS = 20
MAX_NAME_LEN = 60
KEY_PREFIX = "spl_"

_lock = threading.Lock()
_last_used_flush: dict = {}       # token id -> monotonic time of the last write to disk
FLUSH_EVERY_S = 60

# Failed attempts per client address: 10 in 5 minutes locks that address out.
_failures: dict = {}
MAX_FAILURES = 10
FAILURE_WINDOW_S = 300


def _file():
    return persistence.DATA_DIR / "api_tokens.json"


def _load() -> list:
    try:
        data = json.loads(_file().read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(tokens: list) -> None:
    _atomic_write(_file(), json.dumps(tokens, indent=2), mode=0o600)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create(name: str, scope: str) -> tuple[str, dict]:
    """Make a key. Returns (the key, shown once, and its public record)."""
    name = " ".join((name or "").split())[:MAX_NAME_LEN]
    if not name:
        raise ValueError("Give the key a name, e.g. the program that will use it.")
    if scope not in SCOPES:
        raise ValueError("Scope must be 'read' or 'write'.")
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    record = {
        "id": secrets.token_hex(4),
        "name": name,
        "scope": scope,
        "hash": _hash(key),
        "hint": key[:len(KEY_PREFIX) + 4] + "…",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_used": None,
    }
    with _lock:
        tokens = _load()
        if len(tokens) >= MAX_TOKENS:
            raise ValueError(f"At most {MAX_TOKENS} keys; revoke one you no longer use.")
        tokens.append(record)
        _save(tokens)
    return key, _public(record)


def _public(record: dict) -> dict:
    return {k: record.get(k) for k in ("id", "name", "scope", "hint", "created", "last_used")}


def list_tokens() -> list:
    return [_public(t) for t in _load()]


def revoke(token_id: str) -> bool:
    with _lock:
        tokens = _load()
        kept = [t for t in tokens if t.get("id") != token_id]
        if len(kept) == len(tokens):
            return False
        _save(kept)
        return True


def verify(key: str) -> dict | None:
    """The public record of the key's owner, or None. Compares against every
    stored hash in constant time per hash, so timing says nothing about which
    keys exist."""
    if not isinstance(key, str) or not key.startswith(KEY_PREFIX) or len(key) > 200:
        return None
    digest = _hash(key)
    found = None
    for t in _load():
        if hmac.compare_digest(t.get("hash", ""), digest):
            found = t
    if found is None:
        return None
    _touch(found["id"])
    return _public(found)


def _touch(token_id: str) -> None:
    now = time.monotonic()
    if now - _last_used_flush.get(token_id, -1e9) < FLUSH_EVERY_S:
        return
    _last_used_flush[token_id] = now
    with _lock:
        tokens = _load()
        for t in tokens:
            if t.get("id") == token_id:
                t["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save(tokens)


# ── brute-force guard ───────────────────────────────────────────────────────

def is_blocked(client: str) -> bool:
    count, since = _failures.get(client, (0, 0.0))
    if time.monotonic() - since > FAILURE_WINDOW_S:
        _failures.pop(client, None)
        return False
    return count >= MAX_FAILURES


def record_failure(client: str) -> None:
    now = time.monotonic()
    count, since = _failures.get(client, (0, now))
    if now - since > FAILURE_WINDOW_S:
        count, since = 0, now
    _failures[client] = (count + 1, since)


def reset_failures() -> None:
    _failures.clear()
    _last_used_flush.clear()
