"""
Notification channels. Each one turns a notify.Notification into an outgoing
request using only the standard library, with a short timeout, and raises
NotifierError with a readable message on failure (never with a secret in it).

Credentials come from config.py (Settings -> Integrations); a channel is
"configured" when its required fields are set.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

import config
from features import is_enabled

TIMEOUT = 10.0


class NotifierError(Exception):
    pass


def _request(url: str, data: bytes | None = None, headers: dict | None = None,
             method: str | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise NotifierError(f"The service answered HTTP {e.code}.") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise NotifierError(f"Could not reach the service: {reason}") from None


def _multipart(fields: dict, file_field: str, filename: str, content: bytes,
               content_type: str = "image/jpeg") -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
             f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n').encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _check_http_url(value: str, what: str) -> str:
    parsed = urllib.parse.urlparse(value or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise NotifierError(f"{what} must be a full http(s):// address.")
    return value


def _header(value: str) -> str:
    """HTTP header values are latin-1 only. Anything else (our titles contain
    an em dash, names may have emoji) is sent as an RFC 2047 encoded-word,
    which ntfy decodes back to UTF-8."""
    value = value.replace("\r", " ").replace("\n", " ")
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode() + "?="


# ── ntfy ─────────────────────────────────────────────────────────────────────

_NTFY_PRIORITY = {"low": "2", "default": "3", "high": "4", "urgent": "5"}


def _ntfy_send(n) -> None:
    base = _check_http_url(config.get("ntfy.url") or "https://ntfy.sh", "The ntfy server address").rstrip("/")
    topic = (config.get("ntfy.topic") or "").strip()
    if not topic:
        raise NotifierError("No ntfy topic is set.")
    url = f"{base}/{urllib.parse.quote(topic, safe='')}"
    headers = {"Priority": _NTFY_PRIORITY.get(n.priority, "3")}
    token = config.get("ntfy.token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if n.image:
        # Attachment upload: the body is the picture, text goes in headers.
        headers.update({"Filename": "snapshot.jpg", "Title": _header(n.title), "Message": _header(n.body),
                        "Content-Type": "image/jpeg"})
        _request(url, n.image, headers, "PUT")
    else:
        headers["Title"] = _header(n.title)
        _request(url, n.body.encode("utf-8"), headers, "POST")


# ── Telegram ─────────────────────────────────────────────────────────────────

def _telegram_send(n) -> None:
    token = config.get("telegram.token")
    chat = (config.get("telegram.chat_id") or "").strip()
    if not token or not chat:
        raise NotifierError("The Telegram bot token and chat ID must both be set.")
    api = f"https://api.telegram.org/bot{token}"
    text = f"{n.title}\n{n.body}" if n.body else n.title
    if n.image:
        body, ctype = _multipart({"chat_id": chat, "caption": text[:1000]}, "photo", "snapshot.jpg", n.image)
        _request(f"{api}/sendPhoto", body, {"Content-Type": ctype}, "POST")
    else:
        _request(f"{api}/sendMessage", json.dumps({"chat_id": chat, "text": text[:4000]}).encode(),
                 {"Content-Type": "application/json"}, "POST")


# ── Discord ──────────────────────────────────────────────────────────────────

def _discord_send(n) -> None:
    hook = config.get("discord.webhook")
    if not hook:
        raise NotifierError("No Discord webhook address is set.")
    _check_http_url(hook, "The Discord webhook address")
    payload = {"content": (f"**{n.title}**\n{n.body}" if n.body else f"**{n.title}**")[:1900]}
    if n.image:
        body, ctype = _multipart({"payload_json": json.dumps(payload)}, "files[0]", "snapshot.jpg", n.image)
        _request(hook, body, {"Content-Type": ctype}, "POST")
    else:
        _request(hook, json.dumps(payload).encode(), {"Content-Type": "application/json"}, "POST")


# ── Generic webhook ──────────────────────────────────────────────────────────

# Fields may be added within a schema_version, never removed or renamed (docs/external-api.md).
WEBHOOK_SCHEMA_VERSION = 1


def _webhook_send(n) -> None:
    url = config.get("webhook.url")
    if not url:
        raise NotifierError("No webhook address is set.")
    _check_http_url(url, "The webhook address")
    payload = {
        "schema_version": WEBHOOK_SCHEMA_VERSION, "id": n.id, "timestamp": n.timestamp,
        "event": n.event, "printer_id": n.printer_id, "printer": n.printer_name,
        "title": n.title, "body": n.body, "priority": n.priority,
        "time": n.time, "has_image": bool(n.image),
    }
    if n.extra:
        payload["details"] = n.extra
    _request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"}, "POST")


# ── Web Push (the pre-existing channel) ──────────────────────────────────────

def _webpush_send(n) -> None:
    import push
    push.send_push_all(n.title, n.body)


# key -> (feature key, config keys that must be set, sender)
CHANNELS = {
    "webpush":  ("notify_webpush",  (),                                _webpush_send),
    "ntfy":     ("notify_ntfy",     ("ntfy.topic",),                   _ntfy_send),
    "telegram": ("notify_telegram", ("telegram.token", "telegram.chat_id"), _telegram_send),
    "discord":  ("notify_discord",  ("discord.webhook",),              _discord_send),
    "webhook":  ("notify_webhook",  ("webhook.url",),                  _webhook_send),
}


def is_configured(channel: str) -> bool:
    _, keys, _ = CHANNELS[channel]
    return all(bool(config.get(k)) for k in keys)


def active_channels() -> list:
    """Channels that are switched on and (where needed) configured."""
    return [c for c, (feat, _, _) in CHANNELS.items() if is_enabled(feat) and is_configured(c)]


def send(channel: str, n) -> None:
    CHANNELS[channel][2](n)
