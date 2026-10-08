"""notify.py, notifiers.py, snapshot.py and the printer-side event hooks."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import config
import features
import notifiers
import notify
import push
import snapshot
from notify import Notification
from printers.base import PrinterConnection


@pytest.fixture(autouse=True)
def clean_guard():
    notify.reset_spam_guard()
    yield
    notify.reset_spam_guard()


def _settings(**kw):
    push.save_notif_settings(kw)


# ── should_send ──────────────────────────────────────────────────────────────

def test_event_must_be_switched_on():
    n = Notification("print_started", "p1", "t")
    assert not notify.should_send(n)
    _settings(started={"enabled": True})
    assert notify.should_send(n)


def test_master_switch_off_blocks_everything():
    _settings(finished={"enabled": True})
    features.set_enabled("notifications", False)
    assert not notify.should_send(Notification("print_complete", "p1", "t"))


def test_spam_guard_blocks_repeat_but_not_completion_or_error():
    _settings(paused={"enabled": True}, finished={"enabled": True}, error={"enabled": True})
    paused = Notification("print_paused", "p1", "t")
    assert notify.should_send(paused) and not notify.should_send(paused)
    assert notify.should_send(Notification("print_paused", "p2", "t"))      # other printer is fine
    for ev in ("print_complete", "print_complete", "print_error", "print_error"):
        assert notify.should_send(Notification(ev, "p1", "t"))


def test_test_event_ignores_settings():
    assert notify.should_send(Notification("test", "", "t"))


def test_pause_and_error_share_settings_keys():
    assert notify.EVENT_SETTING["print_complete"] == notify.EVENT_SETTING["print_cancelled"] == "finished"
    assert notify.EVENT_SETTING["printer_offline"] == notify.EVENT_SETTING["printer_online"] == "offline"


def test_image_only_when_event_supports_and_asks():
    _settings(finished={"enabled": True, "image": True}, offline={"enabled": True, "image": True},
              paused={"enabled": True})
    assert notify.wants_image("print_complete")
    assert not notify.wants_image("print_paused")        # supports images, not asked for
    assert not notify.wants_image("printer_offline")     # never carries one


# ── channels ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(notifiers, "_request", lambda url, data=None, headers=None, method=None:
                        calls.append({"url": url, "data": data, "headers": headers or {}, "method": method}) or b"{}")
    return calls


def _decode_header(value):
    from email.header import decode_header, make_header
    return str(make_header(decode_header(value)))


def test_ntfy_text_message(sent):
    config.set("ntfy.topic", "my printers")
    config.set("ntfy.token", "tk_secret")
    notifiers.send("ntfy", Notification("print_error", "p1", "Bench — Print error", "Hotend fault", priority="urgent"))
    c = sent[0]
    assert c["url"] == "https://ntfy.sh/my%20printers" and c["method"] == "POST"
    assert c["data"] == b"Hotend fault"
    assert _decode_header(c["headers"]["Title"]) == "Bench — Print error" and c["headers"]["Priority"] == "5"
    assert c["headers"]["Authorization"] == "Bearer tk_secret"


def test_ntfy_picture_goes_as_attachment(sent):
    config.set("ntfy.topic", "t")
    notifiers.send("ntfy", Notification("print_complete", "p1", "Done", "x.gcode", image=b"\xff\xd8JPEG\xff\xd9"))
    c = sent[0]
    assert c["method"] == "PUT" and c["data"].startswith(b"\xff\xd8")
    assert c["headers"]["Filename"] == "snapshot.jpg" and c["headers"]["Message"] == "x.gcode"
    assert _decode_header(c["headers"]["Title"]) == "Done"


def test_ntfy_non_latin_title_is_header_safe(sent):
    config.set("ntfy.topic", "t")
    notifiers.send("ntfy", Notification("x", "p", "Skriver ✓ klar — æøå", "b"))
    sent[0]["headers"]["Title"].encode("latin-1")   # must not raise
    assert _decode_header(sent[0]["headers"]["Title"]) == "Skriver ✓ klar — æøå"


def test_telegram_message_and_photo(sent):
    config.set("telegram.token", "123:ABC")
    config.set("telegram.chat_id", "42")
    notifiers.send("telegram", Notification("x", "p", "Title", "Body"))
    assert sent[0]["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert json.loads(sent[0]["data"]) == {"chat_id": "42", "text": "Title\nBody"}
    notifiers.send("telegram", Notification("x", "p", "Title", "Body", image=b"\xff\xd8x\xff\xd9"))
    assert sent[1]["url"].endswith("/sendPhoto")
    assert b'name="photo"' in sent[1]["data"] and b'name="caption"' in sent[1]["data"]
    assert sent[1]["headers"]["Content-Type"].startswith("multipart/form-data")


def test_discord_json_and_picture(sent):
    config.set("discord.webhook", "https://discord.com/api/webhooks/1/abc")
    notifiers.send("discord", Notification("x", "p", "T", "B"))
    assert json.loads(sent[0]["data"])["content"] == "**T**\nB"
    notifiers.send("discord", Notification("x", "p", "T", "B", image=b"\xff\xd8x\xff\xd9"))
    assert b'name="payload_json"' in sent[1]["data"] and b'name="files[0]"' in sent[1]["data"]


def test_webhook_payload(sent):
    config.set("webhook.url", "https://example.org/hook")
    notifiers.send("webhook", Notification("print_paused", "p1", "T", "B", printer_name="Bench",
                                           extra={"code": 704}, priority="high"))
    body = json.loads(sent[0]["data"])
    assert body["event"] == "print_paused" and body["printer"] == "Bench" and body["details"] == {"code": 704}
    assert body["has_image"] is False


@pytest.mark.parametrize("channel,keys", [
    ("ntfy", {}), ("telegram", {"telegram.token": "x"}), ("discord", {}), ("webhook", {}),
])
def test_missing_settings_give_readable_error(sent, channel, keys):
    for k, v in keys.items():
        config.set(k, v)
    with pytest.raises(notifiers.NotifierError):
        notifiers.send(channel, Notification("x", "p", "T"))
    assert not sent


def test_bad_urls_rejected(sent):
    config.set("webhook.url", "file:///etc/passwd")
    with pytest.raises(notifiers.NotifierError, match="http"):
        notifiers.send("webhook", Notification("x", "p", "T"))
    config.set("discord.webhook", "ftp://x")
    with pytest.raises(notifiers.NotifierError):
        notifiers.send("discord", Notification("x", "p", "T"))


def test_request_errors_never_contain_the_secret_url(monkeypatch):
    import urllib.error
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(notifiers.urllib.request, "urlopen", boom)
    with pytest.raises(notifiers.NotifierError) as e:
        notifiers._request("https://api.telegram.org/botSECRETTOKEN/sendMessage", b"{}")
    assert "SECRETTOKEN" not in str(e.value) and "refused" in str(e.value)


def test_active_channels_need_config_and_feature(monkeypatch):
    assert notifiers.active_channels() == ["webpush"]
    config.set("ntfy.topic", "t")
    assert notifiers.active_channels() == ["webpush", "ntfy"]
    features.set_enabled("notify_ntfy", False)
    assert notifiers.active_channels() == ["webpush"]
    features.set_enabled("notifications", False)
    assert notifiers.active_channels() == []


def test_feature_missing_flag_reflects_config():
    byk = {f["key"]: f for f in features.describe_all()}
    assert byk["notify_ntfy"]["missing"] is True and byk["notify_webpush"]["missing"] is False
    config.set("ntfy.topic", "t")
    assert {f["key"]: f for f in features.describe_all()}["notify_ntfy"]["missing"] is False


def test_failing_channel_does_not_stop_the_others(monkeypatch):
    got = []
    def fake_send(ch, n):
        if ch == "ntfy":
            raise notifiers.NotifierError("down")
        got.append(ch)
    monkeypatch.setattr(notifiers, "send", fake_send)
    notify._deliver(Notification("x", "p", "T"), ["ntfy", "webhook"])
    assert got == ["webhook"]


def test_secrets_are_masked_in_integrations_listing():
    config.set("telegram.token", "123:ABC")
    config.set("discord.webhook", "https://discord.com/api/webhooks/1/abc")
    dump = json.dumps(config.describe_all())
    assert "123:ABC" not in dump and "api/webhooks" not in dump


# ── snapshot ─────────────────────────────────────────────────────────────────

class _Mjpeg(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(_Mjpeg.status)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        if _Mjpeg.status != 200:
            return
        frame = b"\xff\xd8" + b"JPEGDATA" * 50 + b"\xff\xd9"
        try:
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n--frame\r\n" + frame)
            self.wfile.flush()
        except OSError:
            pass

    def log_message(self, *a):
        pass


@pytest.fixture
def camera():
    _Mjpeg.status = 200
    srv = HTTPServer(("127.0.0.1", 0), _Mjpeg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/?action=stream"
    srv.shutdown()


def test_grab_jpeg_returns_first_complete_frame(camera):
    jpg = snapshot.grab_jpeg(camera, timeout=3)
    assert jpg.startswith(b"\xff\xd8") and jpg.endswith(b"\xff\xd9") and b"--frame" not in jpg


def test_grab_jpeg_none_on_error_or_bad_url(camera):
    _Mjpeg.status = 503
    assert snapshot.grab_jpeg(camera, timeout=2) is None
    assert snapshot.grab_jpeg("http://127.0.0.1:1/", timeout=1) is None
    assert snapshot.grab_jpeg("", timeout=1) is None
    assert snapshot.grab_jpeg("file:///etc/passwd", timeout=1) is None


# ── printer-side hooks ───────────────────────────────────────────────────────

@pytest.fixture
def printer(monkeypatch):
    p = PrinterConnection("pid1", "10.0.0.5", "Bench")
    p.connected = True
    captured = []
    monkeypatch.setattr(notify, "is_event_on", lambda event: True)
    monkeypatch.setattr(notify, "notify", lambda n: captured.append(n) or True)
    p.captured = captured
    return p


def _reason(kind="pause", initiated_by="printer", category="unknown", **kw):
    return {"kind": kind, "initiated_by": initiated_by, "category": category,
            "code": kw.get("code", ""), "message": kw.get("message", ""), "action": kw.get("action", ""),
            "raw": {}, "since": "x"}


def test_pause_from_printer_notifies_with_cause(printer):
    printer._notify_reason(_reason(category="thermal", code=103, message="Hotend isn't heating", action="Check it."))
    n = printer.captured[0]
    assert n.event == "print_paused" and n.title == "Bench — Print paused"
    assert "Hotend isn't heating" in n.body and "(code 103)" in n.body and "Check it." in n.body
    assert n.extra["code"] == 103


def test_pause_started_from_spooler_is_silent(printer):
    printer._notify_reason(_reason(initiated_by="spooler"))
    assert printer.captured == []


def test_error_notifies_urgently(printer):
    printer._notify_reason(_reason(kind="error", category="fan", code=701, message="Mainboard fan fault."))
    n = printer.captured[0]
    assert n.event == "print_error" and n.priority == "urgent" and "701" in n.body


def test_filament_runout_has_its_own_event_for_pause_and_error(printer):
    printer._notify_reason(_reason(kind="pause", category="filament_runout", code=1260, message="Filament break."))
    printer._notify_reason(_reason(kind="error", category="filament_runout", code=109))
    assert [n.event for n in printer.captured] == ["filament_runout", "filament_runout"]


def test_error_without_known_cause_still_says_something(printer):
    printer._notify_reason(_reason(kind="error"))
    assert printer.captured[0].body == "The printer reported an error."


@pytest.mark.asyncio
async def test_emit_takes_camera_picture_when_asked(printer, monkeypatch):
    printer.camera_url = "http://cam/"
    monkeypatch.setattr(notify, "wants_image", lambda event: True)
    import printers.base as base
    monkeypatch.setattr(base, "grab_jpeg", lambda url: b"\xff\xd8x\xff\xd9")
    printer._emit("print_complete", "T", "B")
    await asyncio.sleep(0.05)
    assert printer.captured and printer.captured[0].image == b"\xff\xd8x\xff\xd9"


@pytest.mark.asyncio
async def test_offline_notice_comes_after_delay_and_online_after_return(printer, monkeypatch):
    import printers.base as base
    monkeypatch.setattr(notify, "event_settings", lambda event: {"minutes": 5})
    real_sleep = asyncio.sleep
    delays = []
    async def fast_sleep(t):
        delays.append(t)
        await real_sleep(0)
    monkeypatch.setattr(base.asyncio, "sleep", fast_sleep)
    printer.connected = False
    printer._track_connection_for_notifications()
    await real_sleep(0.05)
    assert delays == [300.0]
    assert [n.event for n in printer.captured] == ["printer_offline"]
    printer.connected = True
    printer._track_connection_for_notifications()
    await real_sleep(0.05)
    assert [n.event for n in printer.captured] == ["printer_offline", "printer_online"]


@pytest.mark.asyncio
async def test_quick_reconnect_cancels_offline_notice(printer, monkeypatch):
    import printers.base as base
    monkeypatch.setattr(notify, "event_settings", lambda event: {"minutes": 5})
    printer.connected = False
    printer._track_connection_for_notifications()
    assert printer._offline_task is not None
    printer.connected = True
    printer._track_connection_for_notifications()
    await asyncio.sleep(0.05)
    assert printer.captured == [] and printer._offline_task is None


# ── a real HTTP round trip (nothing mocked) ──────────────────────────────────

class _Hook(BaseHTTPRequestHandler):
    seen = []
    code = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        _Hook.seen.append({"headers": dict(self.headers), "body": self.rfile.read(n)})
        self.send_response(_Hook.code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.fixture
def hook():
    _Hook.seen, _Hook.code = [], 200
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook"
    srv.shutdown()


def test_webhook_round_trip_and_test_message(hook):
    config.set("webhook.url", hook)
    notify.send_test("webhook")
    body = json.loads(_Hook.seen[0]["body"])
    assert body["event"] == "test" and _Hook.seen[0]["headers"]["Content-Type"] == "application/json"


def test_webhook_http_error_is_readable(hook):
    config.set("webhook.url", hook)
    _Hook.code = 500
    with pytest.raises(notifiers.NotifierError, match="HTTP 500"):
        notify.send_test("webhook")


def test_notify_hands_off_to_worker_and_filters(hook, monkeypatch):
    config.set("webhook.url", hook)
    _settings(paused={"enabled": True})
    done = threading.Event()
    real = notify._deliver
    monkeypatch.setattr(notify, "_deliver", lambda n, ch: (real(n, ch), done.set()))
    assert notify.notify(Notification("print_paused", "p1", "T", "B"))
    assert done.wait(3)
    assert any(json.loads(r["body"])["event"] == "print_paused" for r in _Hook.seen)
    assert not notify.notify(Notification("print_started", "p1", "T"))   # event off


def test_print_complete_is_announced_even_if_the_printer_is_busy_just_before_it_goes_idle():
    """Real CC2: history was saved but no 'complete' notification came; its last status before idle was a busy one."""
    push.save_notif_settings({"finished": {"enabled": True}})
    p = PrinterConnection("pid1", "10.0.0.5", "Bench")
    p.emitted = []
    p._emit = lambda event, *a, **k: p.emitted.append(event)

    def at(status):
        p.status = {"PrintInfo": {"Status": status, "Filename": "a.gcode"}}
        p._check_notifications()
    for status in (0, 15, 3, 3, 10, 0, 0):
        at(status)
    assert p.emitted == ["print_complete"]          # once, not again on the second idle
    for status in (1, 0):                           # homing afterwards isn't a print
        at(status)
    assert p.emitted == ["print_complete"]
