"""I6: nothing that can act on the printers works while login is off, and the
external API can never act on a printer, whatever its key allows."""

import io
import json

import pytest

import api_tokens
import auth
import features
import http_handler
import state
from features import Feature, FeatureError


@pytest.fixture
def control_feature(monkeypatch):
    """A stand-in for the future advanced_control: needs login."""
    monkeypatch.setitem(features.FEATURES, "advanced_control", Feature(
        key="advanced_control", name="Advanced control", default=True,
        description="Heat, fans, axes.", requires_auth=True, risky=True))


# ── the login rule ───────────────────────────────────────────────────────────

def test_feature_needing_login_is_forced_off_while_login_is_off(control_feature, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    features.set_enabled("advanced_control", True)
    assert features.is_enabled("advanced_control")
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    assert not features.is_enabled("advanced_control")          # even though it's stored as on
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    assert features.is_enabled("advanced_control")              # and returns when login does


def test_it_cannot_be_switched_on_while_login_is_off(control_feature, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    with pytest.raises(FeatureError, match="Requires login"):
        features.set_enabled("advanced_control", True)
    features.set_enabled("advanced_control", False)             # switching OFF is always allowed


def test_the_features_list_shows_it_locked_with_the_reason(control_feature, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    f = {x["key"]: x for x in features.describe_all()}["advanced_control"]
    assert f["enabled"] is False and f["locked"] is True
    assert f["lock_reason"].startswith("Requires login") and "AUTH_ENABLED=false" in f["lock_reason"]
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    f = {x["key"]: x for x in features.describe_all()}["advanced_control"]
    assert f["locked"] is False and f["lock_reason"] is None


def test_other_features_are_untouched_by_the_login_rule(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    for key, feat in features.FEATURES.items():
        if not feat.requires_auth and key not in ("external_api", "spaghetti_detection", "dataset_collection", "update_check"):
            assert features.is_enabled(key) is True, key
    features.set_enabled("external_api", True)       # needs a key, adds nothing to an open UI
    assert features.is_enabled("external_api")


def test_no_registered_feature_that_acts_on_printers_forgets_the_rule():
    # When T12/T19 add advanced_control / share_links they must declare requires_auth.
    for key in ("advanced_control", "share_links"):
        if key in features.FEATURES:
            assert features.FEATURES[key].requires_auth, key


def test_security_status_reports_the_login_state(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(http_handler, "AUTH_ENABLED", False, raising=False)
    status, body = _call("GET", "/api/security-status")
    assert status == 200 and json.loads(body) == {"auth_enabled": False}


def test_diagnostics_say_when_login_is_off(monkeypatch):
    import diagnostics
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    assert "Login: OFF" in diagnostics.build_report()
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    assert "Login: on" in diagnostics.build_report()


# ── the external API can never drive a printer ───────────────────────────────

class _ForbiddenPrinter:
    """Any attempt to command it fails the test."""
    id, name, printer_type, ip, connected = "pid1", "Bench", "cc1", "10.0.0.5", True

    def __init__(self):
        self.touched = []

    def to_dict(self):
        return {"state": "idle"}

    def __getattr__(self, name):
        if name.startswith("_") or name in ("status",):
            raise AttributeError(name)

        def spy(*a, **k):
            self.touched.append(name)
            raise AssertionError(f"external API called printer.{name}")
        return spy


def _call(method, path, key=None, body=None):
    h = object.__new__(http_handler.SPHandler)
    h.command, h.path, h.request_version, h.requestline = method, path, "HTTP/1.1", f"{method} {path} HTTP/1.1"
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = {"Authorization": f"Bearer {key}" if key else "", "Content-Length": str(len(raw))}
    h.headers = type("H", (dict,), {"get": lambda self, k, d=None: dict.get(self, k, d)})(hdrs)
    h.client_address = ("10.0.0.9", 1234)
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h.server = type("S", (), {})()
    try:
        getattr(h, f"do_{method}")()
    except AttributeError as e:         # a method this handler doesn't implement
        if "do_" not in str(e):
            raise
        return 405, b""
    out = h.wfile.getvalue()
    head, _, payload = out.partition(b"\r\n\r\n")
    return (int(out.split(b" ")[1]) if out else 0), payload


PRINTER_ID = "0" * 32
CONTROL_PATHS = [
    "/api/external/v1/printers/{p}/pause", "/api/external/v1/printers/{p}/resume", "/api/external/v1/printers/{p}/stop",
    "/api/external/v1/printers/{p}/start", "/api/external/v1/printers/{p}/light", "/api/external/v1/printers/{p}/temperature",
    "/api/external/v1/printers/{p}/fan", "/api/external/v1/printers/{p}/move", "/api/external/v1/printers/{p}/home",
    "/api/external/v1/printers/{p}/upload", "/api/external/v1/printers/{p}/files", "/api/external/v1/printers/{p}",
    "/api/external/v1/printer/pause", "/api/external/v1/control", "/api/external/v1/command", "/api/external/v1/start",
    "/api/external/v1/stop", "/api/external/v1/pause", "/api/external/v1/printers", "/api/external/v1/upload",
    "/api/external/v1/history/{p}/start", "/api/external/v1/history/{p}/reprint",
]


@pytest.mark.parametrize("method", ["GET", "PATCH", "POST", "PUT", "DELETE"])
def test_no_external_request_ever_reaches_a_printer_even_with_a_write_key(method, monkeypatch):
    printer = _ForbiddenPrinter()
    monkeypatch.setattr(state, "printers", {"pid1": printer})
    features.set_enabled("external_api", True)
    key, _ = api_tokens.create("everything", "write")
    bodies = [None, {}, {"reference": "x"}, {"command": "pause"}, {"temperature": 250}, {"printer": "pid1", "action": "stop"}]
    for path in CONTROL_PATHS:
        for body in bodies:
            _call(method, path.format(p=PRINTER_ID), key, body)
    assert printer.touched == []


def test_the_external_api_only_has_the_routes_it_documents():
    """If someone adds a route, this fails and forces a conscious decision."""
    import inspect
    src = inspect.getsource(http_handler.SPHandler._handle_external)
    assert src.count('method == "PATCH"') == 1
    assert 'method == "POST"' not in src and 'method == "DELETE"' not in src
    patched = src[src.index('if method == "PATCH"'):src.index("if path == base:")]
    for forbidden in ("send_cmd", "state.printers", "upload_file", "start_print_file", "set_light"):
        assert forbidden not in patched
    # the read side may list printers (no control), nothing else touches them
    reads = src[src.index("if path == base:"):]
    assert "send_cmd" not in reads and "upload_file" not in reads and "start_print_file" not in reads


def test_external_printer_list_exposes_state_only(monkeypatch):
    from printers.cc1 import CC1Connection
    p = CC1Connection("pid1", "10.9.9.9", "Bench", access_code="s3cret")
    monkeypatch.setattr(state, "printers", {"pid1": p})
    features.set_enabled("external_api", True)
    key, _ = api_tokens.create("r", "read")
    status, body = _call("GET", "/api/external/v1/printers", key)
    item = json.loads(body)["items"][0]
    assert status == 200 and set(item) == {"id", "name", "type", "connected", "state"}
    assert b"s3cret" not in body and b"10.9.9.9" not in body


# ── Spoolman through the proxy is only as private as Spooler ─────────────────

@pytest.mark.parametrize("path, status", [
    ("/api/v1/spool", 401), ("/api/v1/filament?limit=5", 401),
    ("/spoolman/", 302), ("/spoolman/spool", 302), ("/spoolman", 302),
])
def test_the_spoolman_proxy_needs_login(path, status, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(http_handler, "proxy_spoolman_enabled", lambda: True)
    proxied = []
    monkeypatch.setattr(http_handler.SPHandler, "_proxy_spoolman_ui",
                        lambda self, *a, **k: proxied.append(a))
    got, _ = _call("GET", path)
    assert got == status and proxied == []


def test_the_spoolman_proxy_still_works_when_signed_in(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(http_handler, "proxy_spoolman_enabled", lambda: True)
    monkeypatch.setattr(http_handler, "_auth_ok", lambda h: True)
    proxied = []
    monkeypatch.setattr(http_handler.SPHandler, "_proxy_spoolman_ui",
                        lambda self, *a, **k: proxied.append(a))
    _call("GET", "/api/v1/spool")
    _call("GET", "/spoolman/")
    assert proxied == [("GET", "/api/v1/spool"), ("GET", "/")]


def test_posting_to_spoolman_needs_the_proxy_switched_on(monkeypatch):
    monkeypatch.setattr(http_handler, "_auth_ok", lambda h: True)
    monkeypatch.setattr(http_handler, "proxy_spoolman_enabled", lambda: False)
    proxied = []
    monkeypatch.setattr(http_handler.SPHandler, "_proxy_spoolman_ui",
                        lambda self, *a, **k: proxied.append(a))
    status, _ = _call("POST", "/api/v1/spool", body={"filament_id": 1})
    assert status == 404 and proxied == []


# ── backups need backup.manage; old sessions end with the account or password ──

def _call_as(method, path, token, body=None):
    h = object.__new__(http_handler.SPHandler)
    h.command, h.path, h.request_version, h.requestline = method, path, "HTTP/1.1", f"{method} {path} HTTP/1.1"
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = {"Cookie": f"{auth.SESSION_COOKIE}={token}" if token else "", "Content-Length": str(len(raw))}
    h.headers = type("H", (dict,), {"get": lambda self, k, d=None: dict.get(self, k, d)})(hdrs)
    h.client_address = ("10.0.0.9", 1234)
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h.server = type("S", (), {})()
    getattr(h, f"do_{method}")()
    out = h.wfile.getvalue()
    return int(out.split(b" ")[1]), out.partition(b"\r\n\r\n")[2]


@pytest.fixture
def accounts(monkeypatch):
    import users
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(http_handler, "AUTH_ENABLED", True)
    users.ensure_initialised()
    users.create_user("boss", "secret12", "admin")
    users.create_user("grandma", "secret12", "viewer")
    features.set_enabled("backup", True)
    return users


@pytest.mark.parametrize("path", ["/api/backup", "/api/backup?include_secrets=1", "/api/backups",
                                  "/api/backups/daily-20260101T000000Z.zip"])
def test_a_viewer_cannot_download_backups(accounts, path):
    status, body = _call_as("GET", path, auth._create_session("grandma"))
    assert status == 403 and json.loads(body)["need"] == "backup.manage"


def test_an_admin_can_still_download_a_backup(accounts):
    status, _ = _call_as("GET", "/api/backups", auth._create_session("boss"))
    assert status == 200


def test_deleting_a_user_ends_their_sessions(accounts):
    gone = auth._create_session("grandma")
    assert _call_as("GET", "/api/printers", gone)[0] == 200
    assert _call_as("DELETE", "/api/users/grandma", auth._create_session("boss"))[0] == 200
    assert _call_as("GET", "/api/printers", gone)[0] == 401


def test_an_admin_password_reset_ends_the_users_sessions(accounts):
    boss = auth._create_session("boss")
    old = auth._create_session("grandma")
    assert _call_as("PATCH", "/api/users/grandma", boss, {"password": "newsecret1"})[0] == 200
    assert _call_as("GET", "/api/printers", old)[0] == 401
    assert _call_as("GET", "/api/printers", boss)[0] == 200


def test_changing_your_own_password_keeps_this_session_and_ends_the_others(accounts):
    here, elsewhere = auth._create_session("grandma"), auth._create_session("grandma")
    assert _call_as("POST", "/api/change-password", here, {"password": "newsecret1"})[0] == 200
    assert _call_as("GET", "/api/printers", here)[0] == 200
    assert _call_as("GET", "/api/printers", elsewhere)[0] == 401
