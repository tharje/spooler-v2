"""
One place every notification goes through.

Callers describe *what happened* (a Notification); this module decides
whether it should be sent (master switch, per-event setting, spam guard),
then hands it to every active channel on a worker thread so a slow or dead
service can never stall printer monitoring or the other channels.
"""

import concurrent.futures
import threading
import uuid
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import notifiers
from features import is_enabled
from push import load_notif_settings

# event -> key in notification_settings.json that switches it on/off
EVENT_SETTING = {
    "print_started":   "started",
    "print_complete":  "finished",
    "print_cancelled": "finished",
    "print_paused":    "paused",
    "print_error":     "error",
    "filament_runout": "filament_runout",
    "printer_offline": "offline",
    "printer_online":  "offline",
    "firmware_changed": "firmware",
    "nozzle_hot_idle": "nozzle_idle",
    "nozzle_overheat": "nozzle_printing",
    "layer_reached":   "layer",
    "spool_low":       "spool_low",
}
# Events that may carry a camera picture (when their setting asks for one).
IMAGE_EVENTS = {"print_started", "print_complete", "print_cancelled", "print_paused",
                "print_error", "filament_runout"}
# Never suppressed by the spam guard: each one is a distinct, important moment.
ALWAYS_SEND = {"print_complete", "print_cancelled", "print_error", "test"}
MIN_INTERVAL_S = 10 * 60

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
_last_sent: dict = {}
_lock = threading.Lock()


@dataclass
class Notification:
    event: str
    printer_id: str
    title: str
    body: str = ""
    image: bytes | None = None
    priority: str = "default"          # low | default | high | urgent
    printer_name: str = ""
    extra: dict = field(default_factory=dict)
    time: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    # One id per event (not per channel), so a receiver can spot the same event twice.
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def event_settings(event: str) -> dict:
    key = EVENT_SETTING.get(event)
    return (load_notif_settings().get(key) or {}) if key else {}


def is_event_on(event: str) -> bool:
    """Cheap pre-check (before a camera picture is taken): master switch on and
    this event switched on. Doesn't touch the spam guard."""
    return is_enabled("notifications") and bool(event_settings(event).get("enabled"))


def wants_image(event: str) -> bool:
    return event in IMAGE_EVENTS and bool(event_settings(event).get("image"))


def should_send(n: Notification) -> bool:
    if not is_enabled("notifications"):
        return False
    if n.event != "test" and not event_settings(n.event).get("enabled"):
        return False
    if n.event not in ALWAYS_SEND:
        key = (n.printer_id, n.event)
        now = time.monotonic()
        with _lock:
            last = _last_sent.get(key)
            if last is not None and now - last < MIN_INTERVAL_S:
                return False
            _last_sent[key] = now
    return True


def _deliver(n: Notification, channels: list) -> None:
    for ch in channels:
        try:
            notifiers.send(ch, n)
        except notifiers.NotifierError as e:
            print(f"[Notify] {ch} failed: {e}")
        except Exception as e:  # a bug in one channel must not hide the others
            print(f"[Notify] {ch} error: {type(e).__name__}")


def notify(n: Notification) -> bool:
    """Queue `n` for every active channel. Returns False when it was filtered
    out (or nothing is listening), True when it was handed to the worker."""
    if not should_send(n):
        return False
    channels = notifiers.active_channels()
    if not channels:
        return False
    _executor.submit(_deliver, n, channels)
    return True


def send_test(channel: str) -> None:
    """Send a test message through one channel right now (not queued), so the
    caller can report success or the readable failure. Ignores event settings."""
    n = Notification(event="test", printer_id="", title="Spooler — test notification",
                     body="If you can read this, this channel works.")
    notifiers.send(channel, n)


def reset_spam_guard() -> None:
    with _lock:
        _last_sent.clear()
