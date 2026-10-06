"""
Configuration store for external-service integrations — lets values be set
from the UI instead of only via .env, without a restart.

Only fields for integrations that actually exist today are registered:
Spoolman (already a real, working integration) and the Slicer link (a small,
self-contained feature specified directly in this task, not a forward
reference to another one). The roadmap sketches further fields for
notification channels, Home Assistant MQTT, and failure-detection — none of
that code exists yet (T10/T16/T17), and those tasks should register their
own Field entries here when actually built, the same way T7's feature
registry only lists features that exist.

Server network/auth settings (HTTP_PORT, AUTH_ENABLED, DATA_DIR, etc.) are
deliberately never represented here — a wrong value for those could lock the
user out, so they stay env-var-only and read-only in the UI.
"""

import json
import os
import stat
import threading
import time
import urllib.parse
from dataclasses import dataclass

from persistence import DATA_DIR, _atomic_write

INTEGRATIONS_FILE = DATA_DIR / "integrations.json"
_lock = threading.Lock()

# "Test connection" results, per integration name -- in-memory only (not
# persisted): a stale test result from a previous run is worse than none, so
# it resets on every restart rather than claiming to still be valid.
_last_test_results: dict = {}


def record_test_result(integration: str, ok: bool, message: str) -> None:
    _last_test_results[integration] = {
        "ok": ok, "message": message, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def last_test_result(integration: str):
    return _last_test_results.get(integration)

# When set, environment variables always win and every field is read-only in
# the UI -- for someone hosting Spooler for other people who shouldn't be able
# to repoint it at a different Spoolman instance etc.
LOCK_CONFIG = os.getenv("SPOOLER_LOCK_CONFIG", "").lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Field:
    key: str            # "spoolman.url"
    label: str
    type: str            # "url" | "text" | "secret" | "int" | "bool"
    env: str = None      # environment variable used as the default value source
    default: object = None
    schemes: tuple = ()  # allowed URL schemes, e.g. ("http", "https")


FIELDS: dict = {
    "spoolman.url": Field(
        key="spoolman.url", label="Spoolman URL", type="url",
        env="SPOOLMAN_URL", default="http://localhost:7912", schemes=("http", "https"),
    ),
    "spoolman.proxy": Field(
        key="spoolman.proxy", label="Proxy Spoolman UI through Spooler", type="bool",
        env="PROXY_SPOOLMAN", default=True,
    ),
    "spoolman.auth_user": Field(
        key="spoolman.auth_user", label="Basic auth username (optional)", type="text", default="",
    ),
    "spoolman.auth_pass": Field(
        key="spoolman.auth_pass", label="Basic auth password (optional)", type="secret", default="",
    ),
    "slicer.url": Field(
        key="slicer.url", label="Slicer URL", type="url", default="", schemes=("http", "https"),
    ),
    "ntfy.url": Field(
        key="ntfy.url", label="ntfy server", type="url", default="https://ntfy.sh",
        schemes=("http", "https"),
    ),
    "ntfy.topic": Field(key="ntfy.topic", label="ntfy topic", type="text", default=""),
    "ntfy.token": Field(key="ntfy.token", label="ntfy access token (optional)", type="secret", default=""),
    "telegram.token": Field(key="telegram.token", label="Telegram bot token", type="secret", default=""),
    "telegram.chat_id": Field(key="telegram.chat_id", label="Telegram chat ID", type="text", default=""),
    "discord.webhook": Field(key="discord.webhook", label="Discord webhook address", type="secret", default=""),
    "webhook.url": Field(key="webhook.url", label="Webhook address", type="secret", default=""),
    "backup.interval_days": Field(
        key="backup.interval_days", label="Automatic backup interval (days)", type="int",
        env="SPOOLER_BACKUP_INTERVAL_DAYS", default=1,
    ),
}

_on_change: list = []  # list of (prefix, callback(key, value))


class ConfigError(Exception):
    pass


def _load_overrides() -> dict:
    try:
        return json.loads(INTEGRATIONS_FILE.read_text())
    except Exception:
        return {}


def _save_overrides(overrides: dict) -> None:
    _atomic_write(INTEGRATIONS_FILE, json.dumps(overrides, indent=2))
    try:
        os.chmod(INTEGRATIONS_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600 -- contains secrets
    except OSError:
        pass


def _coerce_env_string(field: Field, raw: str):
    if field.type == "bool":
        return raw.lower() in ("1", "true", "yes")
    if field.type == "int":
        try:
            return int(raw)
        except ValueError:
            return field.default
    return raw


def _env_value(field: Field):
    if not field.env:
        return None
    raw = os.getenv(field.env)
    if raw is None:
        return None
    return _coerce_env_string(field, raw)


def source_of(key: str) -> str:
    """Which layer a field's current effective value comes from: "ui"
    (stored override), "env" (environment variable), or "default"."""
    field = FIELDS[key]
    if LOCK_CONFIG:
        return "env" if _env_value(field) is not None else "default"
    overrides = _load_overrides()
    if key in overrides:
        return "ui"
    if _env_value(field) is not None:
        return "env"
    return "default"


def get(key: str):
    if key not in FIELDS:
        raise ConfigError(f"Unknown config key: {key!r}")
    field = FIELDS[key]

    if LOCK_CONFIG:
        env_val = _env_value(field)
        return env_val if env_val is not None else field.default

    overrides = _load_overrides()
    if key in overrides:
        return overrides[key]
    env_val = _env_value(field)
    if env_val is not None:
        return env_val
    return field.default


def _validate(field: Field, value):
    if field.type == "url":
        if value == "":
            return value  # clearing an optional URL field is fine
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in field.schemes:
            raise ConfigError(
                f"{field.label}: URL must start with {' or '.join(field.schemes)}://"
            )
        if not parsed.netloc:
            raise ConfigError(f"{field.label}: not a valid URL.")
        return value.rstrip("/")
    if field.type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes")
    if field.type == "int":
        try:
            parsed_int = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{field.label}: must be a number.")
        if parsed_int < 0:
            raise ConfigError(f"{field.label}: cannot be negative.")
        return parsed_int
    return str(value)


def set(key: str, value) -> None:
    if key not in FIELDS:
        raise ConfigError(f"Unknown config key: {key!r}")
    if LOCK_CONFIG:
        raise ConfigError(f"{FIELDS[key].label} is locked by SPOOLER_LOCK_CONFIG.")
    field = FIELDS[key]
    validated = _validate(field, value)
    with _lock:
        overrides = _load_overrides()
        overrides[key] = validated
        _save_overrides(overrides)
    _dispatch(key, validated)


def clear(key: str) -> None:
    """Remove a stored override, falling back to the environment variable or
    default. Used for the dedicated "Remove" action on secret fields, and
    generally for "reset this field" on any field."""
    if key not in FIELDS:
        raise ConfigError(f"Unknown config key: {key!r}")
    if LOCK_CONFIG:
        raise ConfigError(f"{FIELDS[key].label} is locked by SPOOLER_LOCK_CONFIG.")
    with _lock:
        overrides = _load_overrides()
        if key in overrides:
            del overrides[key]
            _save_overrides(overrides)
    _dispatch(key, get(key))


def _dispatch(key: str, value) -> None:
    for prefix, callback in _on_change:
        if key.startswith(prefix):
            try:
                callback(key, value)
            except Exception as e:
                print(f"[Config] on_change callback for {key!r} failed: {e}")


def on_change(prefix: str, callback) -> None:
    """Register callback(key, value) invoked whenever a field whose key
    starts with `prefix` changes via set() or clear() -- e.g. prefix
    "spoolman." for anything Spoolman-related. Lets a service with a
    persistent connection (a future MQTT client, say) reconnect immediately
    instead of needing a restart."""
    _on_change.append((prefix, callback))


def describe_all() -> list:
    """Everything the /api/integrations endpoint and the settings UI need.
    Secret values are never returned in cleartext -- "set" (bool) is given
    instead so the UI can show a masked placeholder without ever seeing the
    real value, regardless of whether it came from the UI or an env var."""
    out = []
    for key, field in FIELDS.items():
        value = get(key)
        entry = {
            "key": key,
            "label": field.label,
            "type": field.type,
            "source": source_of(key),
            "locked": LOCK_CONFIG,
        }
        if field.type == "secret":
            entry["set"] = bool(value)
        else:
            entry["value"] = value
        out.append(entry)
    return out
