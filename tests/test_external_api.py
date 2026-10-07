"""The external API: API keys, the read/write scopes, and what it exposes."""

import io
import json
import uuid

import pytest

import api_tokens
import features
import http_handler
import persistence

JPEG = b"\xff\xd8" + b"x" * 40 + b"\xff\xd9"
A, B, C = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture(autouse=True)
def fresh():
    api_tokens.reset_failures()
    yield
    api_tokens.reset_failures()


@pytest.fixture
def history():
    persistence.append_history({"id": A, "timestamp": "2026-10-06T21:00:00", "printer_id": "p1", "printer_name": "CC2",
                                "filename": "benchy.gcode", "filament_mm": 4000, "filament_g": 12.4, "print_time_s": 3000,
                                "end_state": "complete", "snapshot": True, "reference": "042", "material": "PETG"})
    persistence.append_history({"id": B, "timestamp": "2026-10-05T10:00:00", "printer_id": "p2", "printer_name": "CC1",
                                "filename": "cube.gcode", "filament_mm": 900, "filament_g": 2.7, "print_time_s": 600,
                                "end_state": "error", "error_message": "Hotend isn't heating", "error_code": "103",
                                "initiated_by": "printer"})
    persistence.save_snapshot(A, JPEG)


def call(method, path, key=None, body=None, client="10.0.0.9"):
    """Run one request through the real SPHandler without opening a socket."""
    h = object.__new__(http_handler.SPHandler)
    h.command, h.path, h.request_version, h.requestline = method, path, "HTTP/1.1", f"{method} {path} HTTP/1.1"
    raw = json.dumps(body).encode() if body is not None else b""
    h.headers = {"Authorization": f"Bearer {key}" if key else "", "Content-Length": str(len(raw))}
    h.headers = type("H", (dict,), {"get": lambda self, k, d=None: dict.get(self, k, d)})(h.headers)
    h.client_address = (client, 1234)
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h.server = type("S", (), {})()
    getattr(h, f"do_{method}")()
    out = h.wfile.getvalue()
    head, _, payload = out.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    hdrs = {l.split(b": ", 1)[0].decode().lower(): l.split(b": ", 1)[1].decode() for l in head.split(b"\r\n")[1:]}
    return status, hdrs, payload


def jcall(*a, **k):
    status, hdrs, payload = call(*a, **k)
    return status, json.loads(payload) if payload else None


def enable():
    features.set_enabled("external_api", True)


# ── keys ─────────────────────────────────────────────────────────────────────

def test_key_is_shown_once_and_only_its_hash_is_stored(tmp_path):
    key, rec = api_tokens.create("Home dashboard", "read")
    assert key.startswith("spl_") and len(key) > 40
    stored = (tmp_path / "api_tokens.json").read_text()
    assert key not in stored and api_tokens._hash(key) in stored
    assert (tmp_path / "api_tokens.json").stat().st_mode & 0o077 == 0        # owner only
    assert rec["name"] == "Home dashboard" and "hash" not in rec
    assert key[:20] not in json.dumps(api_tokens.list_tokens())


def test_verify_revoke_and_garbage():
    key, rec = api_tokens.create("x", "write")
    assert api_tokens.verify(key)["id"] == rec["id"]
    for bad in ("", "spl_nope", "nothing", None, 5, "spl_" + "x" * 500):
        assert api_tokens.verify(bad) is None
    assert api_tokens.revoke(rec["id"]) and api_tokens.verify(key) is None
    assert not api_tokens.revoke("deadbeef")


def test_create_validation_and_limit():
    for name, scope in [("", "read"), ("   ", "read"), ("ok", "admin")]:
        with pytest.raises(ValueError):
            api_tokens.create(name, scope)
    for i in range(api_tokens.MAX_TOKENS):
        api_tokens.create(f"k{i}", "read")
    with pytest.raises(ValueError):
        api_tokens.create("one too many", "read")


def test_last_used_is_recorded():
    key, rec = api_tokens.create("x", "read")
    assert api_tokens.list_tokens()[0]["last_used"] is None
    api_tokens.verify(key)
    assert api_tokens.list_tokens()[0]["last_used"]


# ── access rules ─────────────────────────────────────────────────────────────

def test_off_by_default_even_with_a_valid_key(history):
    key, _ = api_tokens.create("x", "read")
    assert not features.is_enabled("external_api")
    assert jcall("GET", "/api/external/v1/history", key)[0] == 403


def test_requires_a_valid_key(history):
    enable()
    assert jcall("GET", "/api/external/v1/history")[0] == 401
    status, hdrs, _ = call("GET", "/api/external/v1/history", "spl_wrong")
    assert status == 401 and "bearer" in hdrs["www-authenticate"].lower()


def test_repeated_failures_lock_the_address_out(history):
    enable()
    key, _ = api_tokens.create("x", "read")
    for _ in range(api_tokens.MAX_FAILURES):
        call("GET", "/api/external/v1/history", "spl_wrong", client="6.6.6.6")
    assert call("GET", "/api/external/v1/history", key, client="6.6.6.6")[0] == 429
    assert call("GET", "/api/external/v1/history", key, client="7.7.7.7")[0] == 200   # others unaffected


def test_a_key_does_not_open_the_rest_of_spooler(history):
    enable()
    key, _ = api_tokens.create("x", "write")
    for path in ("/api/printers", "/api/history", "/api/integrations", "/api/api-tokens", "/api/features"):
        status, _, _ = call("GET", path, key)
        assert status in (401, 302), path


# ── reading ──────────────────────────────────────────────────────────────────

def test_history_list_filters_and_shape(history):
    enable()
    key, _ = api_tokens.create("x", "read")
    s, d = jcall("GET", "/api/external/v1/history", key)
    assert s == 200 and d["total"] == 2 and [i["id"] for i in d["items"]] == [A, B]          # newest first
    first = d["items"][0]
    assert first["file"] == "benchy.gcode" and first["result"] == "complete" and first["reference"] == "042"
    assert first["filament_g"] == 12.4 and first["filament_m"] == 4.0 and first["has_picture"] is True
    assert first["picture_url"] == f"/api/external/v1/history/{A}/picture" and first["cause"] is None
    failed = d["items"][1]
    assert failed["result"] == "error" and failed["cause"]["code"] == "103" and failed["picture_url"] is None
    assert "snapshot" not in first and "completed" not in first          # no internals leak

    assert jcall("GET", "/api/external/v1/history?reference=042", key)[1]["total"] == 1
    assert jcall("GET", "/api/external/v1/history?q=CUBE", key)[1]["items"][0]["id"] == B
    assert jcall("GET", "/api/external/v1/history?result=error", key)[1]["total"] == 1
    assert jcall("GET", "/api/external/v1/history?printer=p1", key)[1]["total"] == 1
    assert jcall("GET", "/api/external/v1/history?from=2026-10-06&to=2026-10-06", key)[1]["total"] == 1
    page = jcall("GET", "/api/external/v1/history?limit=1&offset=1", key)[1]
    assert page["total"] == 2 and [i["id"] for i in page["items"]] == [B]
    assert jcall("GET", "/api/external/v1/history?result=bogus", key)[0] == 400
    assert jcall("GET", "/api/external/v1/history?limit=x", key)[0] == 400


def test_single_print_and_picture(history):
    enable()
    key, _ = api_tokens.create("x", "read")
    assert jcall("GET", f"/api/external/v1/history/{A}", key)[1]["id"] == A
    status, hdrs, body = call("GET", f"/api/external/v1/history/{A}/picture", key)
    assert status == 200 and hdrs["content-type"] == "image/jpeg" and body == JPEG
    assert jcall("GET", f"/api/external/v1/history/{B}/picture", key)[0] == 404      # print without a picture
    assert jcall("GET", f"/api/external/v1/history/{C}", key)[0] == 404
    assert jcall("GET", "/api/external/v1/history/../../etc/passwd", key)[0] == 404


def test_stats_and_index(history):
    enable()
    key, _ = api_tokens.create("x", "read")
    s, d = jcall("GET", "/api/external/v1/stats", key)
    assert s == 200 and d["prints"] == 2 and d["results"]["error"] == 1
    s, d = jcall("GET", "/api/external/v1", key)
    assert s == 200 and d["scope"] == "read" and d["version"] == 1


def test_cors_header_for_browser_programs(history):
    enable()
    key, _ = api_tokens.create("x", "read")
    assert call("GET", "/api/external/v1/history", key)[1]["access-control-allow-origin"] == "*"


# ── writing the reference ───────────────────────────────────────────────────

def test_read_key_cannot_change_but_write_key_can(history):
    enable()
    rk, _ = api_tokens.create("reader", "read")
    wk, _ = api_tokens.create("writer", "write")
    assert jcall("PATCH", f"/api/external/v1/history/{A}", rk, {"reference": "9"})[0] == 403
    assert persistence.load_history()[0]["reference"] == "042"
    s, d = jcall("PATCH", f"/api/external/v1/history/{A}", wk, {"reference": " 7-B "})
    assert s == 200 and d["reference"] == "7-B"
    assert jcall("GET", f"/api/external/v1/history/{A}", rk)[1]["reference"] == "7-B"
    assert jcall("PATCH", f"/api/external/v1/history/{A}", wk, {"reference": ""})[1]["reference"] == ""


def test_write_key_can_only_change_the_reference(history):
    enable()
    wk, _ = api_tokens.create("writer", "write")
    assert jcall("PATCH", f"/api/external/v1/history/{A}", wk, {"filename": "x"})[0] == 400
    assert jcall("PATCH", f"/api/external/v1/history/{A}", wk, {"reference": "x" * 41})[0] == 400
    assert jcall("PATCH", f"/api/external/v1/history/{C}", wk, {"reference": "1"})[0] == 404
    assert jcall("PATCH", "/api/external/v1/stats", wk, {"reference": "1"})[0] == 404
    assert persistence.load_history()[0]["filename"] == "benchy.gcode"


def test_picture_cannot_be_edited_or_deleted(history):
    enable()
    wk, _ = api_tokens.create("writer", "write")
    assert jcall("PATCH", f"/api/external/v1/history/{A}/picture", wk, {"reference": "1"})[0] == 404
    assert call("DELETE", f"/api/external/v1/history/{A}", wk)[0] in (401, 302)
    assert persistence.snapshot_path(A).exists()
