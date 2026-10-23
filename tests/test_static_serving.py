"""http_handler.py — index.html must never be cached (it's the only thing that
names which ?v= of each script to load; a stale cached copy of it keeps a
browser/PWA stuck on old JS even after every asset underneath has updated)."""

import io

import pytest

import http_handler


@pytest.fixture(autouse=True)
def _bypass_auth(monkeypatch):
    monkeypatch.setattr(http_handler, "_auth_ok", lambda handler: True)


def _get(path):
    h = object.__new__(http_handler.SPHandler)
    h.command, h.path, h.request_version, h.requestline = "GET", path, "HTTP/1.1", f"GET {path} HTTP/1.1"
    h.headers = type("H", (dict,), {"get": lambda self, k, d=None: dict.get(self, k, d)})({})
    h.client_address = ("10.0.0.9", 1234)
    h.rfile, h.wfile = io.BytesIO(b""), io.BytesIO()
    h.server = type("S", (), {})()
    h.do_GET()
    head, _, body = h.wfile.getvalue().partition(b"\r\n\r\n")
    return head.decode(), body


def test_root_path_serves_index_html_with_no_cache_header():
    head, body = _get("/")
    assert "Cache-Control: no-cache" in head
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()


def test_index_html_path_also_gets_no_cache_header():
    head, _ = _get("/index.html")
    assert "Cache-Control: no-cache" in head


def test_root_path_with_query_string_still_matches():
    head, _ = _get("/?foo=bar")
    assert "Cache-Control: no-cache" in head
