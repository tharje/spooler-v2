"""Per-user notifications (step A): push subscriptions belong to a user, each
user picks their own events, and nobody is told about a printer they can't see."""

import pytest

import auth
import http_handler
import notifiers
import notify
import push
import users
from notify import Notification
from test_rbac_http import _call, _session


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(http_handler, "AUTH_ENABLED", True, raising=False)
    auth._sessions.clear()
    users.ensure_initialised()
    notify.reset_spam_guard()
    users.create_user("boss", "secret1", "admin")
    users.create_user("kid", "secret1", "operator")
    sent = []
    monkeypatch.setattr(push, "WEBPUSH_AVAILABLE", True)
    monkeypatch.setattr(push, "_vapid", "k")
    monkeypatch.setattr(push, "is_enabled", lambda name: True)
    monkeypatch.setattr(push, "webpush", lambda subscription_info, **kw: sent.append(subscription_info["endpoint"]),
                        raising=False)
    yield sent
    auth._sessions.clear()


def _sub(name, owner):
    push.add_subscription({"endpoint": f"https://push.example.org/{name}", "keys": {}}, owner=owner)


def _ends(sent):
    return sorted(e.rsplit("/", 1)[1] for e in sent)


def test_users_without_own_choices_follow_shared_settings(setup):
    push.save_notif_settings({"finished": {"enabled": True}})
    _sub("boss-phone", "boss")
    _sub("kid-phone", "kid")
    push.send_push_all("t", "b", key="finished", printer_id="p1")
    assert _ends(setup) == ["boss-phone", "kid-phone"]


def test_own_choices_override_shared(setup):
    push.save_notif_settings({"finished": {"enabled": True}})
    push.save_user_event_choices("kid", {"finished": False, "started": True})
    _sub("boss-phone", "boss")
    _sub("kid-phone", "kid")
    push.send_push_all("t", "b", key="finished")
    assert _ends(setup) == ["boss-phone"]
    setup.clear()
    push.send_push_all("t", "b", key="started")
    assert _ends(setup) == ["kid-phone"]


def test_hidden_printer_is_not_announced(setup):
    push.save_notif_settings({"finished": {"enabled": True}})
    users.set_user_printers("kid", ["p2"])
    _sub("boss-phone", "boss")
    _sub("kid-phone", "kid")
    push.send_push_all("t", "b", key="finished", printer_id="p1")
    assert _ends(setup) == ["boss-phone"]


def test_deleted_users_devices_get_nothing(setup):
    push.save_notif_settings({"finished": {"enabled": True}})
    _sub("kid-phone", "kid")
    users.delete_user("kid")
    push.send_push_all("t", "b", key="finished")
    assert setup == []


def test_test_push_goes_only_to_own_devices(setup):
    _sub("boss-phone", "boss")
    _sub("kid-phone", "kid")
    push.send_push_all("t", "b", owner="kid")
    assert _ends(setup) == ["kid-phone"]


def test_resubscribing_moves_device_to_new_owner(setup):
    _sub("tablet", "boss")
    _sub("tablet", "kid")
    assert [s["owner"] for s in push._push_subs] == ["kid"]


def test_unowned_subscriptions_go_to_first_admin():
    push._push_subs[:] = [{"endpoint": "https://push.example.org/old", "keys": {}}]
    push._adopt_unowned_by_first_admin()
    assert push._push_subs[0]["owner"] == "boss"


def test_only_a_users_own_choice_reaches_only_push(monkeypatch):
    """Shared setting off, one user wants it: detection runs, but only web push gets it."""
    push.save_user_event_choices("kid", {"started": True})
    assert notify.is_event_on("print_started")
    monkeypatch.setattr(notifiers, "active_channels", lambda: ["webpush", "telegram"])
    got = []
    monkeypatch.setattr(notify._executor, "submit", lambda fn, n, chans: got.append(chans))
    assert notify.notify(Notification("print_started", "p1", "T"))
    assert got == [["webpush"]]


def test_nobody_wants_it_means_off():
    assert not notify.is_event_on("print_started")
    assert not notify.key_wanted("started")


# ── HTTP ─────────────────────────────────────────────────────────────────────

def test_my_notifications_roundtrip():
    push.save_notif_settings({"finished": {"enabled": True}})
    kid = _session("kid")
    st, d = _call("GET", "/api/my-notifications", kid)
    assert st == 200 and d["custom"] is False and d["events"]["finished"] is True
    st, _ = _call("POST", "/api/my-notifications", kid, {"events": {"finished": False, "layer": True, "bogus": True}})
    assert st == 200
    assert push.user_event_choices("kid") == {"finished": False, "layer": True}
    st, d = _call("GET", "/api/my-notifications", kid)
    assert d["custom"] is True and d["events"]["finished"] is False and d["events"]["layer"] is True
    _call("POST", "/api/my-notifications", kid, {"events": None})
    assert push.user_event_choices("kid") is None


def test_shared_settings_need_settings_manage():
    st, _ = _call("POST", "/api/notification-settings", _session("kid"), {"finished": {"enabled": True}})
    assert st == 403
    st, _ = _call("POST", "/api/notification-settings", _session("boss"), {"finished": {"enabled": True}})
    assert st == 200


def test_subscribe_records_owner():
    st, _ = _call("POST", "/api/push-subscribe", _session("kid"),
                  {"endpoint": "https://push.example.org/x", "keys": {"p256dh": "a", "auth": "b"}})
    assert st == 200
    assert push._push_subs[-1]["owner"] == "kid"


def test_deleting_user_forgets_devices_and_choices():
    _sub("kid-phone", "kid")
    push.save_user_event_choices("kid", {"started": True})
    st, _ = _call("DELETE", "/api/users/kid", _session("boss"))
    assert st == 200
    assert push._push_subs == [] and push.user_event_choices("kid") is None
