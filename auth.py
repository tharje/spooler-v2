"""
Authentication: sessions, password verification, rate limiting.
"""

import json
import os
import secrets
import time
from pathlib import Path

try:
    import bcrypt as _bcrypt
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False

AUTH_ENABLED    = os.getenv("AUTH_ENABLED", "true").lower() != "false"
SPOOLER_USER    = os.getenv("SPOOLER_USERNAME", "")
SPOOLER_PW_HASH = os.getenv("SPOOLER_PW_HASH", "")
# Set SECURE_COOKIES=true when Spooler is behind a TLS-terminating reverse proxy
# (e.g. Traefik/nginx/Caddy) so the session cookie carries the Secure flag.
SECURE_COOKIES  = os.getenv("SECURE_COOKIES", "false").lower() == "true"

SESSION_COOKIE = "spooler_sid"
SESSION_TTL    = 30 * 24 * 3600  # 30 days

_sessions: dict = {}  # token → expiry (Unix epoch)

# Rate limiting: ip → (attempt_count, window_start_timestamp)
_login_attempts: dict = {}
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_S     = 300  # 5 minutes


def set_auth_file(path: Path) -> None:
    global _AUTH_FILE
    _AUTH_FILE = path

_AUTH_FILE: Path = Path("auth.json")  # overridden by server.py at startup


def _load_auth() -> dict:
    try:
        return json.loads(_AUTH_FILE.read_text())
    except Exception:
        return {}


def _has_password() -> bool:
    if SPOOLER_PW_HASH:
        return True
    return bool(_load_auth().get("pw_hash"))


def _get_pw_hash() -> str:
    return SPOOLER_PW_HASH or _load_auth().get("pw_hash", "")


def _get_username() -> str:
    return SPOOLER_USER or _load_auth().get("username", "admin")


def _save_auth(username: str, pw_hash: str) -> None:
    _AUTH_FILE.write_text(json.dumps({"username": username, "pw_hash": pw_hash}))


# ── Session helpers ────────────────────────────────────────────────────────────

def _parse_sid(cookie_header: str) -> str:
    for part in cookie_header.split(";"):
        name, _, val = part.strip().partition("=")
        if name.strip() == SESSION_COOKIE:
            return val.strip()
    return ""


def _validate_session(token: str) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token, 0)
    if time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def _invalidate_session(token: str) -> None:
    _sessions.pop(token, None)


def _auth_ok(handler) -> bool:
    if not AUTH_ENABLED:
        return True
    token = _parse_sid(handler.headers.get("Cookie", ""))
    return _validate_session(token)


# ── Rate limiting ──────────────────────────────────────────────────────────────

def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    count, start = _login_attempts.get(ip, (0, now))
    if now - start > LOGIN_WINDOW_S:
        _login_attempts[ip] = (1, now)
        return True
    if count >= LOGIN_MAX_ATTEMPTS:
        return False
    _login_attempts[ip] = (count + 1, start)
    return True


def _reset_rate_limit(ip: str) -> None:
    _login_attempts.pop(ip, None)


# ── Background cleanup ─────────────────────────────────────────────────────────

async def session_cleanup_loop() -> None:
    import asyncio
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        expired = [t for t, exp in list(_sessions.items()) if now > exp]
        for t in expired:
            _sessions.pop(t, None)
        old_ips = [ip for ip, (_, start) in list(_login_attempts.items())
                   if now - start > LOGIN_WINDOW_S * 2]
        for ip in old_ips:
            _login_attempts.pop(ip, None)
