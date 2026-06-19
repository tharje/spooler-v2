"""
Web Push (VAPID) support — key management, subscriptions, and send helpers.
"""

import json

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

from persistence import DATA_DIR

VAPID_FILE          = DATA_DIR / "vapid_keys.json"
PUSH_SUBS_FILE      = DATA_DIR / "push_subscriptions.json"
NOTIF_SETTINGS_FILE = DATA_DIR / "notification_settings.json"

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
            VAPID_FILE.write_text(json.dumps({
                "private_pem": private_pem,
                "public_key":  _vapid_public_key,
            }))
            print("[Push] Generated new VAPID key pair")

        # pywebpush 2.x requires a Vapid02 object, not a raw PEM string
        _vapid = Vapid02.from_pem(private_pem.encode())

        if PUSH_SUBS_FILE.exists():
            _push_subs = json.loads(PUSH_SUBS_FILE.read_text())
        print(f"[Push] VAPID ready — public key: {_vapid_public_key[:20]}…")
    except Exception as e:
        print(f"[Push] VAPID init failed: {e}")


def get_public_key() -> str:
    return _vapid_public_key


def _save_subs() -> None:
    PUSH_SUBS_FILE.write_text(json.dumps(_push_subs))


def add_subscription(sub: dict) -> None:
    endpoint = sub.get("endpoint", "")
    _push_subs[:] = [s for s in _push_subs if s.get("endpoint") != endpoint]
    _push_subs.append(sub)
    _save_subs()


def remove_subscription(endpoint: str) -> None:
    _push_subs[:] = [s for s in _push_subs if s.get("endpoint") != endpoint]
    _save_subs()


def has_subscriptions() -> bool:
    return bool(_push_subs)


def load_notif_settings() -> dict:
    try:
        return json.loads(NOTIF_SETTINGS_FILE.read_text()) if NOTIF_SETTINGS_FILE.exists() else {}
    except Exception:
        return {}


def save_notif_settings(s: dict) -> None:
    NOTIF_SETTINGS_FILE.write_text(json.dumps(s))


def send_push_all(title: str, body: str) -> None:
    if not WEBPUSH_AVAILABLE or _vapid is None or not _push_subs:
        return
    dead = []
    for sub in list(_push_subs):
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=_vapid,
                vapid_claims={"sub": "mailto:spooler@localhost"},
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None) if e.response else None
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
