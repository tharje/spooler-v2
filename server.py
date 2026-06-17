#!/usr/bin/env python3
"""
Spooler – Elegoo Centauri Carbon GUI backend
Entry point: starts HTTP, HTTPS, and WebSocket servers.
"""

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=value pairs from .env files into os.environ.

    Checks /data/.env (persistent volume) then the app directory.
    Existing env vars (from docker -e or --env-file) are never overwritten.
    """
    candidates = [
        Path(os.getenv("DATA_DIR", "/data")) / ".env",
        Path(__file__).parent / ".env",
    ]
    for path in candidates:
        try:
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = val.strip()
        except FileNotFoundError:
            pass


_load_dotenv()

try:
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    try:
        from websockets.server import serve as ws_serve
    except ImportError:
        ws_serve = None  # type: ignore

import auth
import state
from auth import AUTH_ENABLED, BCRYPT_AVAILABLE, session_cleanup_loop
from http_handler import run_http, run_https
from persistence import DATA_DIR, load_printers, load_tray_map
from printers import PRINTER_TYPES, make_printer
from ws_handler import browser_handler

# Suppress the noisy-but-harmless "did not receive a valid HTTP request"
# error that websockets logs whenever a TCP connection drops before the
# WebSocket handshake completes (browser reconnect races, port scanners, etc.)
class _SuppressIncompleteHandshake(logging.Filter):
    _PHRASES = (
        "did not receive a valid HTTP request",
        "opening handshake failed",
        "invalid Connection header",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._PHRASES)

logging.getLogger("websockets.server").addFilter(_SuppressIncompleteHandshake())
logging.getLogger("websockets.asyncio.server").addFilter(_SuppressIncompleteHandshake())

HTTP_PORT     = int(os.getenv("HTTP_PORT",  "8080"))
HTTPS_PORT    = int(os.getenv("HTTPS_PORT", "8443"))
WS_PORT       = int(os.getenv("WS_PORT",   "8765"))
HTTPS_ENABLED = os.getenv("HTTPS_ENABLED", "true").lower() != "false"

# Wire the auth file path now that DATA_DIR is resolved
auth.set_auth_file(DATA_DIR / "auth.json")


async def main() -> None:
    if ws_serve is None:
        print("\n[ERROR] Please install the 'websockets' package first.")
        print("  python3 -m venv venv && . venv/bin/activate && pip install websockets")
        sys.exit(1)

    if not AUTH_ENABLED:
        print("[Auth] WARNING: AUTH_ENABLED=false – Spooler is open to everyone on the network!")
    elif not BCRYPT_AVAILABLE:
        print("[Auth] WARNING: 'bcrypt' package not installed – authentication will not work.")
        print("       Install it: pip install bcrypt")
    elif not auth._has_password():
        print("[Auth] No password configured – first-time setup required via the web UI")
    else:
        print(f"[Auth] Authentication enabled (user: {auth._get_username()})")

    # Load saved printers
    for entry in load_printers():
        ptype = entry.get("printer_type", "cc1")
        if ptype not in PRINTER_TYPES:
            print(f"[Config] Skipping unknown printer type '{ptype}' for {entry.get('name')}")
            continue
        pc = make_printer(
            ptype,
            entry["id"],
            entry["ip"],
            entry["name"],
            access_code=entry.get("access_code", ""),
        )
        state.printers[pc.id] = pc
        pc._task = asyncio.create_task(pc.start())
    if state.printers:
        print(f"[Config] Loaded {len(state.printers)} saved printer(s)")

    state.tray_map = load_tray_map()

    asyncio.create_task(session_cleanup_loop())

    http_thread = threading.Thread(target=run_http, args=(HTTP_PORT,), daemon=True)
    http_thread.start()
    if HTTPS_ENABLED:
        https_thread = threading.Thread(target=run_https, args=(HTTPS_PORT,), daemon=True)
        https_thread.start()

    print(f"[WS]   Browser WebSocket on ws://0.0.0.0:{WS_PORT}")
    async with ws_serve(browser_handler, "0.0.0.0", WS_PORT, ping_interval=20, ping_timeout=10):
        print("\n" + "─" * 50)
        print("  Spooler is running!")
        print(f"  HTTP:  http://localhost:{HTTP_PORT}")
        if HTTPS_ENABLED:
            print(f"  HTTPS: https://localhost:{HTTPS_PORT}  ← use this for PWA")
        else:
            print("  HTTPS: disabled (HTTPS_ENABLED=false)")
        print("─" * 50 + "\n")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    if "--hash-password" in sys.argv:
        if not BCRYPT_AVAILABLE:
            print("Error: bcrypt is not installed. Run: pip install bcrypt")
            sys.exit(1)
        import getpass
        import bcrypt as _bcrypt  # type: ignore
        pw  = getpass.getpass("Password: ")
        pw2 = getpass.getpass("Confirm:  ")
        if pw != pw2:
            print("Passwords do not match.")
            sys.exit(1)
        print(_bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode())
        sys.exit(0)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
