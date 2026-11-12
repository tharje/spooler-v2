"""
Web Push (VAPID) support — key management, subscriptions, and send helpers.
"""

import json
import os

try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid02
    from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption,
    )
    import base64 as _b64
    WEBPUSH_AVAILABLE = True
except ImportError:
    WEBPUSH_AVAILABLE = False

from features import is_enabled
from persistence import DATA_DIR, _atomic_write

# Contact address sent to push services (RFC 8292 "sub" claim). Set VAPID_CONTACT
# to e.g. mailto:you@example.org if you want push providers to be able to reach you.
VAPID_CONTACT = os.getenv("VAPID_CONTACT", "mailto:admin@example.com")

VAPID_FILE          = DATA_DIR / "vapid_keys.json"
PUSH_SUBS_FILE      = DATA_DIR / "push_subscriptions.json"
NOTIF_SETTINGS_FILE = DATA_DIR / "notification_settings.json"
# Per-user event choices: {username: {setting key: bool}}. A user with no entry
# follows the shared notification_settings.json.
NOTIF_PREFS_FILE    = DATA_DIR / "notification_prefs.json"

_vapid: "Vapid02 | None" = None
_vapid_public_key: str = ""
_push_subs: list = []


def init_vapid() -> None:
    global _vapid, _vapid_public_key, _push_subs
    if not WEBPUSH_AVAILABLE:
        print("[Push] pywebpush not installed — push notifications disabled")
        return
    try:
        if VAPID_FILE.exists():
            d = json.loads(VAPID_FILE.read_text())
            private_pem    = d["private_pem"]
            _vapid_public_key = d["public_key"]
        else:
            pk = generate_private_key(SECP256R1(), default_backend())
            private_pem = pk.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            ).decode()
            pub = pk.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
            _vapid_public_key = _b64.urlsafe_b64encode(pub).rstrip(b"=").decode()
            _atomic_write(VAPID_FILE, json.dumps({
                "private_pem": private_pem,
                "public_key":  _vapid_public_key,
            }), mode=0o600)  # contains the VAPID private key
            print("[Push] Generated new VAPID key pair")

        # pywebpush 2.x requires a Vapid02 object, not a raw PEM string
        _vapid = Vapid02.from_pem(private_pem.encode())

        if PUSH_SUBS_FILE.exists():
            _push_subs = json.loads(PUSH_SUBS_FILE.read_text())
            _adopt_unowned_by_first_admin()
        print(f"[Push] VAPID ready — public key: {_vapid_public_key[:20]}…")
    except Exception as e:
        print(f"[Push] VAPID init failed: {e}")


def get_public_key() -> str:
    return _vapid_public_key


def _save_subs() -> None:
    PUSH_SUBS_FILE.write_text(json.dumps(_push_subs))


def add_subscription(sub: dict, owner: str = "") -> None:
    """Store a browser's subscription as belonging to `owner` (the signed-in
    user; "" when login is off). Re-subscribing moves it to the new owner."""
    endpoint = sub.get("endpoint", "")
    _push_subs[:] = [s for s in _push_subs if s.get("endpoint") != endpoint]
    _push_subs.append({"endpoint": endpoint, "keys": sub.get("keys") or {}, "owner": owner or ""})
    _save_subs()


def remove_subscription(endpoint: str) -> None:
    _push_subs[:] = [s for s in _push_subs if s.get("endpoint") != endpoint]
    _save_subs()


def remove_owner(owner: str) -> None:
    """Forget a deleted user's devices and event choices."""
    _push_subs[:] = [s for s in _push_subs if s.get("owner") != owner]
    _save_subs()
    prefs = load_notif_prefs()
    if prefs.pop(owner, None) is not None:
        _atomic_write(NOTIF_PREFS_FILE, json.dumps(prefs))


def adopt_unowned(owner: str) -> None:
    """Subscriptions from before per-user notifications have no owner: give
    them to `owner` (the first admin) so they keep getting what they got."""
    if owner and any(not s.get("owner") for s in _push_subs):
        for s in _push_subs:
            if not s.get("owner"):
                s["owner"] = owner
        _save_subs()


def _adopt_unowned_by_first_admin() -> None:
    from auth import AUTH_ENABLED
    if not AUTH_ENABLED:
        return
    import users
    admins = [u for u in users.list_users() if u.get("role") == users.ADMIN_ROLE]
    if admins:
        adopt_unowned(min(admins, key=lambda u: u.get("created") or "")["username"])


def has_subscriptions(owner: str | None = None) -> bool:
    return any(owner is None or s.get("owner", "") == owner for s in _push_subs)


def load_notif_settings() -> dict:
    try:
        return json.loads(NOTIF_SETTINGS_FILE.read_text()) if NOTIF_SETTINGS_FILE.exists() else {}
    except Exception:
        return {}


def save_notif_settings(s: dict) -> None:
    NOTIF_SETTINGS_FILE.write_text(json.dumps(s))


def load_notif_prefs() -> dict:
    try:
        d = json.loads(NOTIF_PREFS_FILE.read_text()) if NOTIF_PREFS_FILE.exists() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def user_event_choices(owner: str) -> dict | None:
    """This user's own {setting key: bool}, or None when they follow the shared settings."""
    own = load_notif_prefs().get(owner)
    return own if isinstance(own, dict) else None


def save_user_event_choices(owner: str, choices: dict | None) -> None:
    """None goes back to following the shared settings."""
    prefs = load_notif_prefs()
    if choices is None:
        prefs.pop(owner, None)
    else:
        prefs[owner] = {str(k): bool(v) for k, v in choices.items()}
    _atomic_write(NOTIF_PREFS_FILE, json.dumps(prefs))


def user_wants(owner: str, key: str | None, shared: dict | None = None) -> bool:
    """Whether `owner` wants events of setting `key` on their devices. No key
    (a test) is always wanted."""
    if not key:
        return True
    own = user_event_choices(owner) if owner else None
    if own is not None:
        return bool(own.get(key))
    shared = load_notif_settings() if shared is None else shared
    return bool((shared.get(key) or {}).get("enabled"))


def any_user_wants(key: str) -> bool:
    """Someone's own choices switch `key` on (the shared settings are checked separately)."""
    return any(isinstance(c, dict) and c.get(key) for c in load_notif_prefs().values())


def send_push_all(title: str, body: str, key: str | None = None, printer_id: str = "",
                  owner: str | None = None) -> None:
    """Push to every subscribed device whose owner wants setting `key` and may
    see `printer_id`. `owner` limits it to one user's devices (a test)."""
    if not is_enabled("notify_webpush") or not WEBPUSH_AVAILABLE or _vapid is None or not _push_subs:
        return
    shared = load_notif_settings()
    allowed: dict = {}

    def _allowed(who: str) -> bool:
        if who not in allowed:
            allowed[who] = _may_receive(who, key, printer_id, shared)
        return allowed[who]

    dead = []
    for sub in list(_push_subs):
        who = sub.get("owner", "")
        if owner is not None and who != owner:
            continue
        if not _allowed(who):
            continue
        try:
            webpush(
                subscription_info={"endpoint": sub.get("endpoint"), "keys": sub.get("keys") or {}},
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=_vapid,
                vapid_claims={"sub": VAPID_CONTACT},
            )
        except WebPushException as e:
            # `is not None`: a requests.Response is falsy for 4xx/5xx, so `if e.response` never matched.
            status = getattr(e.response, "status_code", None) if e.response is not None else None
            if status in (404, 410):
                dead.append(sub)
            else:
                print(f"[Push] WebPush error: {e}")
        except Exception as e:
            print(f"[Push] Send error: {e}")
    if dead:
        for d in dead:
            try:
                _push_subs.remove(d)
            except ValueError:
                pass
        _save_subs()


def _may_receive(who: str, key: str | None, printer_id: str, shared: dict) -> bool:
    if not who:
        # Login is off (or the device predates accounts): the shared settings decide.
        return not key or bool((shared.get(key) or {}).get("enabled"))
    import users
    if users.get_user(who) is None:
        return False                      # deleted account
    if printer_id and not users.can_see_printer(who, printer_id):
        return False
    return user_wants(who, key, shared)
