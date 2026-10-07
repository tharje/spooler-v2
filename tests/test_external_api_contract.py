"""The shape of every external API response is a promise (docs/external-api.md).

These tests pin field names and types. Adding fields is allowed within v1, so
extra fields are ignored; a field that disappears or changes type fails here.
If one of these fails, you are about to break somebody's integration: add a
field instead, or introduce /api/external/v2.
"""

import io
import json
import re
from pathlib import Path

import pytest

import api_tokens
import features
import http_handler
import http_external
import persistence
import state

S, N, B, NULL = str, (int, float), bool, type(None)
A, E = "a" * 32, "e" * 32          # a finished print with everything, a failed one with little

ITEM = {
    "id": S, "ended_at": S, "started_at": (S, NULL), "printer_id": (S, NULL), "printer_name": (S, NULL),
    "file": (S, NULL), "result": S, "result_label": S, "cause": (dict, NULL),
    "print_time_s": N, "filament_mm": N, "filament_m": N, "filament_g": N,
    "material": (S, NULL), "vendor": (S, NULL), "spools": list, "pauses": list,
    "reference": S, "has_picture": B, "picture_url": (S, NULL),
}
CAUSE = {"text": S, "message": (S, NULL), "category": (S, NULL), "category_label": (S, NULL),
         "code": (S, NULL), "initiated_by": (S, NULL), "initiated_by_label": (S, NULL)}
SPOOL = {"id": (int, NULL), "g": (N, NULL), "name": (S, NULL), "material": (S, NULL), "vendor": (S, NULL),
         "color_hex": (S, NULL)}
PAUSE = {"since": (S, NULL), "until": (S, NULL), "duration_s": (N, NULL), "category": (S, NULL),
         "category_label": (S, NULL), "initiated_by": (S, NULL), "initiated_by_label": (S, NULL)}
LIST = {"total": int, "limit": int, "offset": int, "items": list}
INDEX = {"name": S, "version": int, "scope": S, "endpoints": list}
STATS = {"prints": int, "results": dict, "success_rate": (N, NULL), "hours": N, "grams": N, "avg_print_s": (N, NULL),
         "by_material": dict, "by_printer": dict, "failure_reasons": dict, "bucket": S, "series": list,
         "from": (S, NULL), "to": (S, NULL)}
RESULTS = {"complete": int, "cancelled": int, "error": int}
BY_PRINTER = {"prints": int, "hours": N, "grams": N}
SERIES = {"key": S, "prints": int, "grams": N, "hours": N}
PRINTERS = {"items": list}
PRINTER = {"id": S, "name": S, "type": S, "connected": B, "state": (S, NULL)}
PATCHED = {"ok": B, "reference": S}
ERROR = {"error": S}


def conforms(obj, schema, where="response"):
    assert isinstance(obj, dict), f"{where} is not an object"
    for name, typ in schema.items():
        assert name in obj, f"{where}: field '{name}' is missing"
        # bool is an int in Python; never let True pass for a number field
        if typ in (N, int) and isinstance(obj[name], bool):
            raise AssertionError(f"{where}.{name} is a bool, expected a number")
        assert isinstance(obj[name], typ), f"{where}.{name} is {type(obj[name]).__name__}, expected {typ}"


def call(method, path, key=None, body=None, length=None):
    h = object.__new__(http_handler.SPHandler)
    h.command, h.path, h.request_version, h.requestline = method, path, "HTTP/1.1", f"{method} {path} HTTP/1.1"
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = {"Authorization": f"Bearer {key}" if key else "",
            "Content-Length": str(len(raw)) if length is None else length}
    h.headers = type("H", (dict,), {"get": lambda self, k, d=None: dict.get(self, k, d)})(hdrs)
    h.client_address = ("10.0.0.9", 1234)
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h.server = type("S", (), {})()
    getattr(h, f"do_{method}")()
    out = h.wfile.getvalue()
    head, _, payload = out.partition(b"\r\n\r\n")
    hdr = {l.split(b": ", 1)[0].decode().lower(): l.split(b": ", 1)[1].decode() for l in head.split(b"\r\n")[1:]}
    return int(head.split(b" ")[1]), hdr, payload


def jcall(*a, **k):
    status, hdr, payload = call(*a, **k)
    return status, json.loads(payload)


@pytest.fixture
def api(monkeypatch):
    features.set_enabled("external_api", True)
    persistence.append_history({
        "id": A, "timestamp": "2026-10-06T21:00:00", "started_at": "2026-10-06T20:00:00", "printer_id": "p1",
        "printer_name": "CC2", "filename": "benchy.gcode", "filament_mm": 4000, "filament_g": 12.4,
        "print_time_s": 3000, "end_state": "complete", "snapshot": True, "reference": "042", "material": "PETG",
        "vendor": "Elegoo", "spools": [{"id": 2, "g": 12.4}],
        "pauses": [{"since": "2026-10-06T20:30:00", "until": "2026-10-06T20:33:00", "duration_s": 180,
                    "initiated_by": "printer", "category": "filament_runout"}]})
    persistence.append_history({
        "id": E, "timestamp": "2026-10-05T10:00:00", "printer_id": "p2", "printer_name": "CC1", "filename": "cube.gcode",
        "filament_mm": 900, "filament_g": 2.7, "print_time_s": 600, "end_state": "error",
        "error_message": "Hotend isn't heating", "error_code": "103", "stop_reason": "thermal", "initiated_by": "printer"})
    persistence.save_snapshot(A, b"\xff\xd8x\xff\xd9")
    from printers.cc1 import CC1Connection
    monkeypatch.setattr(state, "printers", {"p2": CC1Connection("p2", "10.0.0.2", "CC1")})
    monkeypatch.setattr(http_external, "get_spool_index", lambda: {2: {"name": "Elegoo PETG", "material": "PETG",
                                                                      "vendor": "Elegoo", "color_hex": "FF0000"}})
    read, _ = api_tokens.create("r", "read")
    write, _ = api_tokens.create("w", "write")
    api_tokens.reset_failures()
    return type("Api", (), {"read": read, "write": write})()


B1 = "/api/external/v1"


def test_index(api):
    s, d = jcall("GET", B1, api.read)
    conforms(d, INDEX, "GET /")
    assert s == 200 and d["version"] == 1 and d["scope"] == "read"
    assert jcall("GET", B1, api.write)[1]["scope"] == "write"
    assert all(isinstance(e, str) for e in d["endpoints"])


def test_history_list_and_items(api):
    s, d = jcall("GET", f"{B1}/history", api.read)
    conforms(d, LIST, "GET /history")
    assert s == 200 and d["total"] == 2
    ok, failed = d["items"]
    conforms(ok, ITEM, "item")
    conforms(failed, ITEM, "failed item")
    assert ok["result"] == "complete" and ok["cause"] is None and ok["has_picture"] is True
    assert ok["picture_url"] == f"{B1}/history/{A}/picture"
    for sp in ok["spools"]:
        conforms(sp, SPOOL, "spool")
    for p in ok["pauses"]:
        conforms(p, PAUSE, "pause")
    assert ok["spools"][0]["name"] == "Elegoo PETG" and ok["pauses"][0]["category_label"] == "Filament runout"
    assert failed["result"] == "error"
    conforms(failed["cause"], CAUSE, "cause")
    assert failed["cause"]["text"] == "Hotend isn't heating (code 103)"


def test_single_print_has_the_same_shape_and_raw_is_optional(api):
    s, one = jcall("GET", f"{B1}/history/{A}", api.read)
    conforms(one, ITEM, "GET /history/{id}")
    assert "raw" not in one
    s, with_raw = jcall("GET", f"{B1}/history/{A}?include=raw", api.read)
    assert isinstance(with_raw["raw"], dict) and with_raw["raw"]["id"] == A


def test_enumerations(api):
    items = jcall("GET", f"{B1}/history", api.read)[1]["items"]
    assert {i["result"] for i in items} <= {"complete", "cancelled", "error"}
    assert {i["result_label"] for i in items} <= {"Finished", "Stopped", "Failed"}
    assert jcall("GET", f"{B1}/stats", api.read)[1]["bucket"] in {"day", "week", "month"}
    assert jcall("GET", B1, api.read)[1]["scope"] in {"read", "write"}


def test_picture_is_a_jpeg(api):
    status, hdr, body = call("GET", f"{B1}/history/{A}/picture", api.read)
    assert status == 200 and hdr["content-type"] == "image/jpeg" and body.startswith(b"\xff\xd8")


def test_stats(api):
    s, d = jcall("GET", f"{B1}/stats", api.read)
    conforms(d, STATS, "GET /stats")
    conforms(d["results"], RESULTS, "stats.results")
    for v in d["by_printer"].values():
        conforms(v, BY_PRINTER, "stats.by_printer[]")
    for b in d["series"]:
        conforms(b, SERIES, "stats.series[]")
    assert d["prints"] == 2 and d["results"]["error"] == 1


def test_stats_on_an_empty_history_keeps_the_shape(api, tmp_path):
    (tmp_path / "history.json").write_text("[]")
    s, d = jcall("GET", f"{B1}/stats", api.read)
    conforms(d, STATS, "empty stats")
    assert d["prints"] == 0 and d["success_rate"] is None and d["avg_print_s"] is None


def test_printers(api):
    s, d = jcall("GET", f"{B1}/printers", api.read)
    conforms(d, PRINTERS, "GET /printers")
    assert s == 200 and len(d["items"]) == 1
    conforms(d["items"][0], PRINTER, "printer")


def test_patch_response(api):
    s, d = jcall("PATCH", f"{B1}/history/{A}", api.write, {"reference": " 7 "})
    conforms(d, PATCHED, "PATCH /history/{id}")
    assert s == 200 and d == {"ok": True, "reference": "7"}


@pytest.mark.parametrize("method,path,key_name,body,status", [
    ("GET", f"{B1}/history", None, None, 401),
    ("GET", f"{B1}/history", "bad", None, 401),
    ("GET", f"{B1}/history?limit=x", "read", None, 400),
    ("GET", f"{B1}/history?result=nope", "read", None, 400),
    ("GET", f"{B1}/history?order=up", "read", None, 400),
    ("GET", f"{B1}/history?ended_after=banana", "read", None, 400),
    ("GET", f"{B1}/history/{'c' * 32}", "read", None, 404),
    ("GET", f"{B1}/history/{E}/picture", "read", None, 404),
    ("GET", f"{B1}/nothing", "read", None, 404),
    ("PATCH", f"{B1}/history/{A}", "read", {"reference": "1"}, 403),
    ("PATCH", f"{B1}/history/{A}", "write", {"filename": "x"}, 400),
    ("PATCH", f"{B1}/history/{A}", "write", {"reference": "x" * 41}, 400),
    ("PATCH", f"{B1}/history/{'c' * 32}", "write", {"reference": "1"}, 404),
])
def test_errors_are_json_with_an_error_field(api, method, path, key_name, body, status):
    key = {"read": api.read, "write": api.write, "bad": "spl_wrong", None: None}[key_name]
    s, hdr, payload = call(method, path, key, body)
    assert s == status
    conforms(json.loads(payload), ERROR, f"{method} {path}")
    assert "json" in hdr["content-type"]
    if status == 401:
        assert "bearer" in hdr["www-authenticate"].lower()


def test_feature_off_is_403_with_the_documented_body(api):
    features.set_enabled("external_api", False)
    s, d = jcall("GET", f"{B1}/history", api.read)
    assert s == 403 and d == {"error": "feature_disabled", "feature": "external_api"}


def test_lockout_is_429(api):
    for _ in range(api_tokens.MAX_FAILURES):
        call("GET", f"{B1}/history", "spl_wrong")
    s, d = jcall("GET", f"{B1}/history", api.read)
    conforms(d, ERROR, "429")
    assert s == 429


def test_oversized_or_malformed_patch_bodies_are_refused(api):
    s, hdr, payload = call("PATCH", f"{B1}/history/{A}", api.write, {"reference": "1"}, length=str(17 * 1024))
    assert s == 413 and "error" in json.loads(payload)
    s, hdr, payload = call("PATCH", f"{B1}/history/{A}", api.write, {"reference": "1"}, length="abc")
    assert s == 400 and "error" in json.loads(payload)


def test_every_documented_endpoint_exists_in_the_docs_and_the_index():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "external-api.md").read_text()
    for needle in ("GET /history", "GET /history/{id}", "GET /history/{id}/picture", "PATCH /history/{id}",
                   "GET /stats", "GET /printers", "ended_after", "order", "include", "Versioning and compatibility",
                   "never controls printers", "/api/external/v2", "413", "429"):
        assert needle in doc, needle
    served = {e.split(" ", 1)[1] for e in jcall_index()}
    assert {"/history", "/history/{id}", "/history/{id}/picture", "/stats", "/printers"} <= served


def jcall_index():
    features.set_enabled("external_api", True)
    key, _ = api_tokens.create("i", "read")
    return jcall("GET", B1, key)[1]["endpoints"]
