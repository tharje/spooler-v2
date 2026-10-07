"""A push subscription the push service says is gone (404/410) must be dropped, not retried forever."""

import pytest

import features
import push


class _Resp:
    def __init__(self, code):
        self.status_code = code

    def __bool__(self):          # like requests.Response: falsy for 4xx/5xx
        return self.status_code < 400


@pytest.mark.parametrize("code", [404, 410])
def test_gone_subscription_is_removed(monkeypatch, code):
    sub = {"endpoint": "https://push.example.org/abc", "keys": {}}
    monkeypatch.setattr(push, "WEBPUSH_AVAILABLE", True)
    monkeypatch.setattr(push, "_vapid", "k")
    monkeypatch.setattr(push, "_push_subs", [sub])
    monkeypatch.setattr(push, "_save_subs", lambda: None)
    monkeypatch.setattr(push, "is_enabled", lambda name: True)

    class _Gone(Exception):
        def __init__(self, response):
            self.response = response

    def boom(**kw):
        raise _Gone(_Resp(code))
    # pywebpush may not be installed where the tests run; stand in for it.
    monkeypatch.setattr(push, "WebPushException", _Gone, raising=False)
    monkeypatch.setattr(push, "webpush", boom, raising=False)
    push.send_push_all("t", "b")
    assert push._push_subs == []
