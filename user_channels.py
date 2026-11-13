"""
Each user's own notification channels (ntfy, Telegram, Discord, webhook).

Stored per user in notification_channels.json (0600: it holds tokens and
webhook addresses). A user's channels get the events that user picked
(push.user_wants) for printers they can see, on top of the shared channels
set up under Settings -> Notifications by an admin.
"""

import json
import threading

import notifiers
import persistence
from persistence import _atomic_write

PERMISSION = "notify.channels"
# Fields that are never sent back to the browser, only whether they are set.
SECRET_KEYS = {"ntfy.token", "telegram.token", "discord.webhook", "webhook.url"}
MAX_VALUE_LEN = 500

_lock = threading.RLock()


def _file():
    return persistence.DATA_DIR / "notification_channels.json"


def _load() -> dict:
    try:
        f = _file()
        d = json.loads(f.read_text()) if f.exists() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    _atomic_write(_file(), json.dumps(data), mode=0o600)


def channels_of(user: str) -> dict:
    """{channel: {"enabled": bool, "values": {key: value}}} for one user."""
    with _lock:
        own = _load().get(user)
    return own if isinstance(own, dict) else {}


def is_configured(channel: str, values: dict) -> bool:
    get = notifiers.user_getter(values, channel)
    _, keys, _ = notifiers.CHANNELS[channel]
    return all(bool(get(k)) for k in keys)


def update(user: str, channel: str, enabled: bool | None = None,
           values: dict | None = None, clear=()) -> None:
    """Change one of `user`'s channels. In `values`, a blank secret means
    "unchanged" (the browser never sees the old one); `clear` removes keys."""
    if channel not in notifiers.USER_CHANNELS:
        raise ValueError("Unknown channel.")
    allowed = notifiers.USER_CHANNELS[channel]
    with _lock:
        data = _load()
        ch = data.setdefault(user, {}).setdefault(channel, {"enabled": False, "values": {}})
        vals = ch.setdefault("values", {})
        for k, v in (values or {}).items():
            if k not in allowed:
                raise ValueError(f"{k} is not a setting of {channel}.")
            v = str(v or "").strip()
            if len(v) > MAX_VALUE_LEN:
                raise ValueError("That value is too long.")
            if not v and k in SECRET_KEYS:
                continue
            if v:
                vals[k] = v
            else:
                vals.pop(k, None)
        for k in clear:
            if k in allowed:
                vals.pop(k, None)
        if enabled is not None:
            ch["enabled"] = bool(enabled)
        _save(data)


def describe(user: str) -> dict:
    """What the browser may see: plain values, and only whether secrets are set."""
    own = channels_of(user)
    out = {}
    for channel, keys in notifiers.USER_CHANNELS.items():
        ch = own.get(channel) or {}
        vals = ch.get("values") or {}
        out[channel] = {
            "enabled": bool(ch.get("enabled")),
            "configured": is_configured(channel, vals),
            "fields": [{"key": k, "secret": k in SECRET_KEYS,
                        "set": bool(vals.get(k)),
                        "value": "" if k in SECRET_KEYS else vals.get(k, "")} for k in keys],
        }
    return out


def active(user: str) -> list[tuple[str, object]]:
    """[(channel, settings lookup)] for the user's switched-on, set-up channels."""
    out = []
    for channel, ch in channels_of(user).items():
        if channel in notifiers.USER_CHANNELS and isinstance(ch, dict) and ch.get("enabled"):
            vals = ch.get("values") or {}
            if is_configured(channel, vals):
                out.append((channel, notifiers.user_getter(vals, channel)))
    return out


def users_with_channels() -> list[str]:
    with _lock:
        return [u for u, chans in _load().items() if isinstance(chans, dict)
                and any(isinstance(c, dict) and c.get("enabled") for c in chans.values())]


def remove_user(user: str) -> None:
    with _lock:
        data = _load()
        if data.pop(user, None) is not None:
            _save(data)
