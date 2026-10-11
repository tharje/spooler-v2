"""Spaghetti detection: one-time picture addresses, the detector call, and when an alert fires."""

import json
import time

import pytest

import config
import spaghetti
from spaghetti import Watch

S = {"threshold": 50, "frames": 3}


def test_picture_address_works_once():
    t = spaghetti.offer_image(b"jpeg")
    assert spaghetti.take_image(t) == b"jpeg"
    assert spaghetti.take_image(t) is None
    assert spaghetti.take_image("guess") is None


def test_picture_address_expires(monkeypatch):
    t = spaghetti.offer_image(b"jpeg")
    real = time.monotonic
    monkeypatch.setattr(spaghetti.time, "monotonic", lambda: real() + spaghetti.IMAGE_TTL_S + 1)
    assert spaghetti.take_image(t) is None


def test_best_confidence():
    assert spaghetti.best_confidence([]) == 0
    assert spaghetti.best_confidence(None) == 0
    assert spaghetti.best_confidence([["failure", 0.3, [1, 2, 3, 4]], ["failure", 0.7, [1, 2, 3, 4]], "junk"]) == 0.7


def test_alert_needs_pictures_in_a_row_and_fires_once_per_print():
    w = Watch()
    assert [w.feed(c, S) for c in (0.9, 0.9, 0.2, 0.9, 0.9)] == [False] * 5      # a good picture breaks the row
    assert w.feed(0.9, S) is True
    assert w.feed(0.9, S) is False                                               # already announced
    w.reset()                                                                    # next print
    assert [w.feed(0.9, S) for _ in range(3)] == [False, False, True]


def test_below_the_sensitivity_never_alerts():
    w = Watch()
    assert not any(w.feed(0.49, S) for _ in range(10))


def test_settings_are_clamped(monkeypatch):
    config.set("spaghetti.threshold", 5)
    config.set("spaghetti.frames", 99)
    config.set("spaghetti.interval_s", 1)
    s = spaghetti.settings()
    assert (s["threshold"], s["frames"], s["interval_s"]) == (10, 10, 15)


def test_detector_call_sends_token_and_a_one_time_address(monkeypatch):
    config.set("spaghetti.url", "http://ml.example.com:3333")
    config.set("spaghetti.token", "secret")
    config.set("spaghetti.spooler_url", "http://spooler.example.com:8080")
    seen = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n): return json.dumps({"detections": [["failure", 0.8, [1, 2, 3, 4]]]}).encode()

    def fake_open(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        import urllib.parse as up
        img = up.unquote(req.full_url.split("img=")[1])
        seen["served"] = spaghetti.take_image(img.rsplit("/", 1)[1])
        return Resp()

    monkeypatch.setattr(spaghetti.urllib.request, "urlopen", fake_open)
    assert spaghetti.best_confidence(spaghetti.ask_detector(b"frame")) == 0.8
    assert seen["auth"] == "Bearer secret"
    assert seen["url"].startswith("http://ml.example.com:3333/p/?img=http%3A%2F%2Fspooler.example.com%3A8080%2Fapi%2Fspaghetti-image%2F")
    assert seen["served"] == b"frame"


def test_detector_errors_are_readable(monkeypatch):
    with pytest.raises(spaghetti.DetectError):
        spaghetti.ask_detector(b"x")                       # nothing configured
    config.set("spaghetti.url", "http://ml.example.com")
    config.set("spaghetti.spooler_url", "http://spooler.example.com:8080")

    def refuse(req, timeout):
        raise spaghetti.urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
    monkeypatch.setattr(spaghetti.urllib.request, "urlopen", refuse)
    assert "token" in spaghetti.test_connection()["message"]


def test_card_status_follows_the_checks(monkeypatch):
    import asyncio
    import features

    class P:
        id, name = "p1", "CC1"
        status_code = 13
        emitted = []
        def _decoded_printinfo(self): return {"Status": self.status_code}
        async def _camera_picture(self, max_age_s=0): return b"jpeg"
        async def _broadcast_state(self): pass
        def _emit(self, *a, **k): self.emitted.append(a[0])

    features.set_enabled("spaghetti_detection", True)
    p, answers = P(), []
    monkeypatch.setattr(spaghetti, "ask_detector", lambda jpeg, s: answers.pop(0))
    spaghetti._status.clear(); spaghetti._watches.clear()
    run = lambda: asyncio.run(spaghetti.check_printer(p, {"threshold": 50, "frames": 2}))

    answers.append([]);                        run(); assert spaghetti.status_for("p1")["state"] == "ok"
    answers.append([["failure", 0.9, []]]);    run(); assert spaghetti.status_for("p1")["state"] == "suspect"
    answers.append([["failure", 0.9, []]]);    run(); assert spaghetti.status_for("p1")["state"] == "alert"
    assert p.emitted == ["spaghetti"]
    p.status_code = 0;                         run(); assert spaghetti.status_for("p1") is None
    features.set_enabled("spaghetti_detection", False)
    assert spaghetti.status_for("p1") is None
