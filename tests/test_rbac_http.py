"""HTTP-level checks for RBAC: the permission gate, the users/roles admin API,
auth-status exposing the caller's role, and job-cancel ownership."""

import io
import json

import pytest

import auth
import http_admin
import http_handler
import job_queue
import users


@pytest.fixture(autouse=True)
def login_on(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(http_handler, "AUTH_ENABLED", True, raising=False)
    auth._sessions.clear()
    users.ensure_initialised()
    yield
    auth._sessions.clear()


def _session(username: str) -> str:
    return auth._create_session(username)


def _call(method, path, cookie=None, body=None):
    h = object.__new__(http_handler.SPHandler)
    h.command, h.path, h.request_version, h.requestline = method, path, "HTTP/1.1", f"{method} {path} HTTP/1.1"
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = {"Content-Length": str(len(raw))}
    if cookie:
        hdrs["Cookie"] = f"{auth.SESSION_COOKIE}={cookie}"
    h.headers = type("H", (dict,), {"get": lambda self, k, d=None: dict.get(self, k, d)})(hdrs)
    h.client_address = ("10.0.0.9", 1234)
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h.server = type("S", (), {})()
    getattr(h, f"do_{method}")()
    out = h.wfile.getvalue()
    _, _, payload = out.partition(b"\r\n\r\n")
    status = int(out.split(b" ")[1]) if out else 0
    try:
        data = json.loads(payload)
    except Exception:
        data = None
    return status, data


@pytest.fixture
def people():
    users.create_user("boss", "secret1", "admin")
    users.create_user("kid", "secret1", "operator")
    users.create_user("gran", "secret1", "viewer")
    return {"boss": _session("boss"), "kid": _session("kid"), "gran": _session("gran")}


# ── the permission gate ──────────────────────────────────────────────────────

def test_users_endpoint_requires_users_manage(people):
    status, data = _call("GET", "/api/users", people["boss"])
    assert status == 200 and any(u["username"] == "kid" for u in data["users"])

    status, data = _call("GET", "/api/users", people["kid"])
    assert status == 403 and data["need"] == "users.manage"

    status, _ = _call("GET", "/api/users", people["gran"])
    assert status == 403


def test_no_cookie_is_unauthorized():
    status, _ = _call("GET", "/api/users")
    assert status == 401


def test_auth_status_reports_role_and_permissions(people):
    status, data = _call("GET", "/api/auth-status", people["kid"])
    assert status == 200
    assert data["user"] == "kid" and data["role"] == "operator"
    assert "print.start" in data["permissions"] and "users.manage" not in data["permissions"]


# ── users admin API ──────────────────────────────────────────────────────────

def test_admin_can_create_update_delete_user(people):
    status, data = _call("POST", "/api/users", people["boss"],
                         {"username": "newkid", "password": "secret1", "role": "viewer"})
    assert status == 200 and data["user"]["role"] == "viewer"

    status, _ = _call("PATCH", "/api/users/newkid", people["boss"], {"role": "operator"})
    assert status == 200 and users.get_user("newkid")["role"] == "operator"

    status, _ = _call("DELETE", "/api/users/newkid", people["boss"])
    assert status == 200 and users.get_user("newkid") is None


def test_non_admin_cannot_create_user(people):
    status, _ = _call("POST", "/api/users", people["kid"],
                      {"username": "x", "password": "secret1", "role": "viewer"})
    assert status == 403
    assert users.get_user("x") is None


def test_create_user_bad_input_is_400(people):
    status, data = _call("POST", "/api/users", people["boss"],
                         {"username": "ok", "password": "short", "role": "viewer"})
    assert status == 400 and "error" in data


# ── roles admin API ──────────────────────────────────────────────────────────

def test_admin_can_create_and_edit_roles(people):
    status, data = _call("GET", "/api/roles", people["boss"])
    assert status == 200
    keys = {p["key"] for p in data["permissions"]}
    assert "job.cancel_any" in keys and "users.manage" in keys

    status, _ = _call("POST", "/api/roles", people["boss"],
                      {"name": "printonly", "label": "Print only",
                       "permissions": ["view", "print.queue", "print.start"]})
    assert status == 200
    role = next(r for r in users.list_roles() if r["name"] == "printonly")
    assert set(role["permissions"]) == {"view", "print.queue", "print.start"}

    status, _ = _call("PATCH", "/api/roles/printonly", people["boss"], {"permissions": ["view"]})
    assert status == 200
    role = next(r for r in users.list_roles() if r["name"] == "printonly")
    assert role["permissions"] == ["view"]


def test_viewer_cannot_touch_roles(people):
    status, _ = _call("POST", "/api/roles", people["gran"],
                      {"name": "sneaky", "permissions": ["users.manage"]})
    assert status == 403
    assert all(r["name"] != "sneaky" for r in users.list_roles())


# ── job cancel ownership ─────────────────────────────────────────────────────

def _fake_job(owner):
    return {"id": "job1", "owner": owner, "status": "queued"}


def test_operator_can_cancel_own_job_only(people, monkeypatch):
    import features
    monkeypatch.setattr(http_admin, "job_queue", job_queue)
    monkeypatch.setattr(features, "is_enabled", lambda *_: True)

    deleted = []
    monkeypatch.setattr(job_queue, "get_job", lambda jid: _fake_job("kid"))
    monkeypatch.setattr(job_queue, "delete_job", lambda jid: deleted.append(jid))

    status, _ = _call("DELETE", "/api/queue/job1", people["kid"])
    assert status == 200 and deleted == ["job1"]


def test_operator_cannot_cancel_someone_elses_job(people, monkeypatch):
    import features
    monkeypatch.setattr(features, "is_enabled", lambda *_: True)
    monkeypatch.setattr(job_queue, "get_job", lambda jid: _fake_job("boss"))
    monkeypatch.setattr(job_queue, "delete_job", lambda jid: (_ for _ in ()).throw(AssertionError("deleted")))

    status, data = _call("DELETE", "/api/queue/job1", people["kid"])
    assert status == 403 and data["need"] == "job.cancel_any"


def test_admin_can_cancel_anyones_job(people, monkeypatch):
    import features
    monkeypatch.setattr(features, "is_enabled", lambda *_: True)
    deleted = []
    monkeypatch.setattr(job_queue, "get_job", lambda jid: _fake_job("kid"))
    monkeypatch.setattr(job_queue, "delete_job", lambda jid: deleted.append(jid))

    status, _ = _call("DELETE", "/api/queue/job1", people["boss"])
    assert status == 200 and deleted == ["job1"]


# ── printer visibility ───────────────────────────────────────────────────────

class _FakePrinter:
    def __init__(self, pid):
        self.id = pid
        self.name = pid
        self.camera_url = ""
    def to_dict(self):
        return {"id": self.id, "name": self.name, "state": "idle"}


@pytest.fixture
def two_printers(monkeypatch):
    import state
    monkeypatch.setattr(state, "printers", {"p1": _FakePrinter("p1"), "p2": _FakePrinter("p2")})


def _restricted_user():
    users.create_role("shopa", ["view", "print.start"], printers=["p1"])
    users.create_user("worker", "secret1", "shopa")
    return _session("worker")


def test_printers_list_is_filtered_by_visibility(two_printers):
    worker = _restricted_user()
    status, data = _call("GET", "/api/printers", worker)
    assert status == 200 and {p["id"] for p in data} == {"p1"}


def test_admin_sees_all_printers_over_http(two_printers):
    users.create_user("boss", "secret1", "admin")
    status, data = _call("GET", "/api/printers", _session("boss"))
    assert status == 200 and {p["id"] for p in data} == {"p1", "p2"}


def test_hidden_printer_camera_is_404(two_printers):
    worker = _restricted_user()
    status, _ = _call("GET", "/api/camera/p2", worker)
    assert status == 404


def test_per_user_printer_override_in_api(two_printers):
    users.create_user("boss", "secret1", "admin")
    boss = _session("boss")
    users.create_user("worker", "secret1", "viewer")   # viewer role sees all by default
    _call("PATCH", "/api/users/worker", boss, {"printers": ["p2"]})
    status, data = _call("GET", "/api/printers", _session("worker"))
    assert status == 200 and {p["id"] for p in data} == {"p2"}
