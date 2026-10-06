"""
Feature flag registry — toggle non-core functionality on/off, enforced on
the server (not just hidden in the UI).

Only features that actually exist in the codebase today are registered here:
camera, spoolman, notifications, notify_webpush. The project's roadmap
sketches a much larger eventual list (print snapshots, maintenance
reminders, failure detection, Home Assistant, a job queue, ...), but none of
that code exists yet — those belong to later tasks, which should register
their own Feature entries here when they're actually built, rather than this
module pre-declaring switches for functionality that isn't there to switch.

Core functionality is never gated here: login, printer connection/discovery,
status display (including pause/error reason), print control (start/pause/
resume/stop), the file browser, and the settings page itself always work
regardless of feature state.
"""

import functools
import json
import os
import threading
from dataclasses import dataclass

from persistence import DATA_DIR, _atomic_write

FEATURES_FILE = DATA_DIR / "features.json"
_lock = threading.Lock()


@dataclass(frozen=True)
class Feature:
    key: str
    name: str
    description: str
    default: bool
    requires: tuple = ()          # other feature keys that must be on first
    requires_config: tuple = ()   # config keys that must be set (T8 -- none yet)
    risky: bool = False           # UI should confirm before turning this on


FEATURES: dict = {
    "camera": Feature(
        key="camera", name="Camera", default=True,
        description="Live camera feed on each printer card.",
    ),
    "backup": Feature(
        key="backup", name="Backup & restore", default=True,
        description="Manual backup/restore and automatic daily + pre-upgrade backups.",
    ),
    "spoolman": Feature(
        key="spoolman", name="Spoolman integration", default=True,
        description="Filament spool inventory, tray linking, and automatic deduction after each print.",
    ),
    "file_upload": Feature(
        key="file_upload", name="File upload", default=True,
        description="Upload G-code from the browser to a printer (CC1, Moonraker, PrusaLink).",
    ),
    "report_problem": Feature(
        key="report_problem", name="Report problem", default=True,
        description="Footer button that builds a redacted diagnostics report and opens a prefilled GitHub issue.",
    ),
    "notifications": Feature(
        key="notifications", name="Notifications", default=True,
        description="Master switch for all print notifications.",
    ),
    "notify_webpush": Feature(
        key="notify_webpush", name="Web push notifications", default=True,
        description="Browser push notifications (the only channel today).",
        requires=("notifications",),
    ),
}

# A force-disabled feature can never be turned on, by anyone, until the
# environment variable itself changes (which needs a restart) -- this is an
# operator-level override, not a user setting.
_FORCE_DISABLED = {
    k.strip() for k in os.getenv("SPOOLER_FORCE_DISABLE", "").split(",") if k.strip()
}

_on_change_callbacks: dict = {}  # key -> list[callable(bool)]


class FeatureError(Exception):
    pass


def _load_overrides() -> dict:
    try:
        return json.loads(FEATURES_FILE.read_text())
    except Exception:
        return {}


def is_force_disabled(key: str) -> bool:
    return key in _FORCE_DISABLED


def _requires_satisfied(key: str, overrides: dict) -> bool:
    feat = FEATURES[key]
    return all(_effective_enabled(r, overrides) for r in feat.requires)


def _effective_enabled(key: str, overrides: dict) -> bool:
    if key not in FEATURES:
        return False
    if is_force_disabled(key):
        return False
    stored = overrides.get(key)
    enabled = stored if stored is not None else FEATURES[key].default
    if enabled and not _requires_satisfied(key, overrides):
        return False
    return enabled


def is_enabled(key: str) -> bool:
    if key not in FEATURES:
        raise FeatureError(f"Unknown feature: {key!r}")
    return _effective_enabled(key, _load_overrides())


def _dependents_of(key: str) -> list:
    return [k for k, f in FEATURES.items() if key in f.requires]


def set_enabled(key: str, value: bool) -> list:
    """Turn a feature on or off. Returns the list of other feature keys that
    were cascaded off as a side effect (empty if none). Raises FeatureError
    if the key is unknown, force-disabled, or being turned on while a
    required feature is off.
    """
    if key not in FEATURES:
        raise FeatureError(f"Unknown feature: {key!r}")
    if is_force_disabled(key):
        raise FeatureError(f"{key!r} is locked off by SPOOLER_FORCE_DISABLE.")

    with _lock:
        overrides = _load_overrides()

        if value and not _requires_satisfied(key, overrides):
            missing = [r for r in FEATURES[key].requires if not _effective_enabled(r, overrides)]
            raise FeatureError(f"Cannot enable {key!r}: requires {missing} to be enabled first.")

        cascaded = []
        overrides[key] = value
        if not value:
            # Turning a feature off must also turn off anything that depends
            # on it, recursively, so the UI never shows an "enabled" switch
            # for a feature whose prerequisite just vanished.
            queue = [key]
            while queue:
                current = queue.pop()
                for dependent in _dependents_of(current):
                    if overrides.get(dependent, FEATURES[dependent].default):
                        overrides[dependent] = False
                        cascaded.append(dependent)
                        queue.append(dependent)

        _atomic_write(FEATURES_FILE, json.dumps(overrides, indent=2))

    for changed_key in [key] + cascaded:
        for cb in _on_change_callbacks.get(changed_key, []):
            try:
                cb(is_enabled(changed_key))
            except Exception as e:
                print(f"[Features] on_change callback for {changed_key!r} failed: {e}")

    return cascaded


def on_change(key: str, callback) -> None:
    """Register a callback(enabled: bool) invoked whenever `key`'s effective
    state changes via set_enabled() (including cascaded changes). Lets a
    background job start/stop immediately instead of needing a restart --
    nothing currently registered here has such a job, but the registry pattern
    is ready for the first feature that does (e.g. a future MQTT connection)."""
    _on_change_callbacks.setdefault(key, []).append(callback)


def requires_feature(key: str):
    """Decorator for SPHandler (http_handler.py) methods: reject with 403
    {"error": "feature_disabled", "feature": key} when the feature is off,
    otherwise call through unchanged. The handler instance's own _json()
    helper is used for the rejection response."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            if not is_enabled(key):
                self._json({"error": "feature_disabled", "feature": key}, 403)
                return
            return fn(self, *args, **kwargs)
        wrapper._feature_gate = key  # introspectable for tests
        return wrapper
    return decorator


def describe_all() -> list:
    """Everything the /api/features endpoint and the settings UI need."""
    overrides = _load_overrides()
    out = []
    for key, feat in FEATURES.items():
        out.append({
            "key": key,
            "name": feat.name,
            "description": feat.description,
            "enabled": _effective_enabled(key, overrides),
            "locked": is_force_disabled(key),
            "missing": bool(feat.requires_config),  # none have config requirements yet (T8)
            "risky": feat.risky,
            "requires": list(feat.requires),
        })
    return out
