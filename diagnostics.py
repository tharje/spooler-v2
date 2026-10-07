"""
Diagnostics for the "Report problem" button: a ring buffer of recent log
lines plus a redacted, human-readable report the user can read, copy and
attach to a GitHub issue themselves. Nothing here is ever sent anywhere
automatically.

Redaction is deliberately aggressive: every value we know to be sensitive
(printer IPs, access codes, serial/board IDs, hostnames, printer names) is
replaced literally, then pattern rules catch IPv4 addresses, MAC addresses
and long hex/token-looking strings that slipped through.
"""

import collections
import json
import platform
import re
import sys
import threading
import time

import state
from features import _auth_on, describe_all
from persistence import current_version

LOG_LINES = 400
_buffer: collections.deque = collections.deque(maxlen=LOG_LINES)
_buffer_lock = threading.Lock()

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_LONG_TOKEN = re.compile(r"\b[0-9A-Za-z_\-]{24,}\b")
_COOKIE = re.compile(r"(?i)(token|cookie|authorization|api[_-]?key|password|access[_-]?code)(\"?\s*[:=]\s*\"?)[^\s\"',;}]+")
_SENSITIVE_ATTR_KEYS = ("serial", "mainboard", "hostname", "mac", "uuid", "sn")


class _Tee:
    """Wraps a text stream, keeping a copy of every complete line."""

    def __init__(self, stream):
        self._stream = stream
        self._partial = ""

    def write(self, data):
        n = self._stream.write(data)
        text = self._partial + data
        *lines, self._partial = text.split("\n")
        if lines:
            ts = time.strftime("%H:%M:%S")
            with _buffer_lock:
                for line in lines:
                    if line.strip():
                        _buffer.append(f"{ts} {line}")
        return n

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_log_capture() -> None:
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout)
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr)


def recent_log_lines() -> list:
    with _buffer_lock:
        return list(_buffer)


def _known_secrets() -> list:
    """Literal values that must never appear in a report, longest first so
    a value that contains another one is replaced whole."""
    secrets = set()
    for p in list(state.printers.values()):
        for v in (p.ip, p.access_code, p.mainboard_id, p.name):
            if v and len(str(v)) >= 3:
                secrets.add(str(v))
        for k, v in (p.attrs or {}).items():
            if isinstance(v, str) and v and any(s in k.lower() for s in _SENSITIVE_ATTR_KEYS):
                if len(v) >= 3:
                    secrets.add(v)
    return sorted(secrets, key=len, reverse=True)


def redact(text: str, secrets: list | None = None) -> str:
    for s in (secrets if secrets is not None else _known_secrets()):
        text = text.replace(s, "<redacted>")
    text = _COOKIE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = _MAC.sub("<mac>", text)
    text = _IPV4.sub("<ip>", text)
    text = _LONG_TOKEN.sub("<token>", text)
    return text


def printer_summary(p) -> dict:
    """Identifying fields (name, IP, serial, board ID, hostname) are
    deliberately left out; only what helps reproduce a protocol problem."""
    d = p.to_dict()
    attrs = d.get("attrs") or {}
    return {
        "type": d["printer_type"],
        "model": attrs.get("Model") or attrs.get("MachineName") or "",
        "firmware": attrs.get("FirmwareVersion") or "",
        "connected": d["connected"],
        "state": d["state"],
        "state_reason": d.get("state_reason"),
        "supports_upload": d.get("supports_upload"),
        "camera_connected": d.get("camera_connected"),
    }


def build_report(printer_id: str | None = None) -> str:
    """printer_id None/"" = all printers, "none" = no printer, otherwise
    only the printer with that id."""
    secrets = _known_secrets()
    features = {f["key"]: f["enabled"] for f in describe_all()}
    if printer_id == "none":
        selected = []
    elif printer_id:
        selected = [p for k, p in list(state.printers.items()) if k == printer_id]
    else:
        selected = list(state.printers.values())
    printers = [printer_summary(p) for p in selected]
    raw_status = {}
    for i, p in enumerate(selected, 1):
        raw_status[f"printer_{i}"] = redact(json.dumps(p.status, default=str)[:3000], secrets)
    parts = [
        "Spooler diagnostics",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Spooler version: {current_version()}",
        f"Python: {platform.python_version()}  OS: {platform.system()} {platform.release()}",
        f"Login: {'on' if _auth_on() else 'OFF (AUTH_ENABLED=false)'}",
        "",
        "== Features ==",
        json.dumps(features, indent=2),
        "",
        "== Printers (names, IPs and serial numbers omitted) ==",
        json.dumps(printers, indent=2, default=str),
        "",
        "== Last raw status per printer (redacted, truncated) ==",
        json.dumps(raw_status, indent=2),
        "",
        f"== Last {LOG_LINES} log lines (redacted) ==",
        redact("\n".join(recent_log_lines()), secrets),
        "",
    ]
    return "\n".join(parts)
