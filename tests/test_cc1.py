"""printers/cc1.py -- CC1 status phases, command rejections, reason hint."""

import asyncio
import json

import pytest

from printers.cc1 import CC1Connection


@pytest.fixture
def printer():
    p = CC1Connection("pid1", "10.0.0.5", "Test CC1")
    p.connected = True
    return p


def _status(p, current, print_status=0, **extra):
    p.status = {"CurrentStatus": current, "PrintInfo": {"Status": print_status, **extra}}
    p._update_phase()


@pytest.mark.parametrize("current,print_status,expected_state,expected_phase", [
    ([0], 0, "idle", ""),
    ([4], 0, "preparing", "Self-check"),
    ([5], 0, "preparing", "Leveling"),
    ([6], 0, "preparing", "Resonance test"),
    ([9], 0, "preparing", "Homing"),
    ([10], 0, "preparing", "Unloading filament"),
    ([11], 0, "preparing", "PID calibration"),
    ([2, 5], 0, "preparing", "Leveling"),   # file transfer + leveling: second entry decides
    ([2], 0, "idle", ""),                   # plain file transfer is not "busy"
    ([1], 11, "preparing", "Checking printer"),
    ([1], 17, "preparing", "Resonance test"),
    ([1], 13, "printing", ""),
])
def test_cc1_state_and_phase(printer, current, print_status, expected_state, expected_phase):
    _status(printer, current, print_status)
    d = printer.to_dict()
    assert d["state"] == expected_state
    assert d["phase"] == expected_phase


def test_reason_hint_none_when_not_in_error(printer):
    _status(printer, [0], 8)
    assert printer._protocol_reason_hint() is None


def test_reason_hint_for_status_14_points_to_the_screen(printer):
    _status(printer, [0], 14)
    hint = printer._protocol_reason_hint()
    assert hint["category"] == "unknown" and "screen" in hint["message"]
    assert hint["raw"]["Status"] == 14


def test_reason_hint_keeps_nonzero_error_number_raw(printer):
    _status(printer, [0], 8, ErrorNumber=3)
    hint = printer._protocol_reason_hint()
    assert hint["code"] == 3 and hint["category"] == "unknown" and hint["message"] == ""


class _Broadcasts:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_rejected_start_command_is_reported_to_browsers(printer, monkeypatch):
    import state
    b = _Broadcasts()
    monkeypatch.setattr(state, "broadcast_to_browsers", b)
    raw = json.dumps({"Data": {"Cmd": 128, "Data": {"Ack": 2}}})
    await printer._handle_message(raw)
    errors = [m for m in b.sent if m.get("type") == "error"]
    assert errors and "Start print" in errors[0]["message"] and "wasn't found" in errors[0]["message"]


@pytest.mark.asyncio
async def test_accepted_command_stays_silent(printer, monkeypatch):
    import state
    b = _Broadcasts()
    monkeypatch.setattr(state, "broadcast_to_browsers", b)
    await printer._handle_message(json.dumps({"Data": {"Cmd": 129, "Data": {"Ack": 0}}}))
    assert not [m for m in b.sent if m.get("type") == "error"]


def test_cc1_error_catalog_matches_screen_codes():
    from printers.error_codes import CC1_ERROR_CODES, lookup
    # Confirmed 2026-10-21 by reading every ErrorCode:N string in the
    # official CC1 firmware source -- this is the complete set.
    assert set(CC1_ERROR_CODES) == {101, 102, 103, 104, 302, 303, 304, 401, 402, 502, 701, 702, 703}
    assert lookup(502, "cc1")["category"] == "leveling"
    # 402 is CC1-only (CC1 splits the accelerometer per axis; CC2 has one 401).
    # 502 used to be the example of a "not-a-CC2 code", but the .13 screen shows
    # CC2 carries 502 too, so it's a CC2 code now -- 402 is the clean CC1-only one.
    assert lookup(402, "cc2") is None
    assert lookup(704, "cc1") is None


def _start_acks(printer, monkeypatch, acks):
    """Make send_cmd answer each start command with the next Ack in `acks`
    (None: no answer), the way the printer's reply would arrive."""
    import printers.cc1 as cc1
    monkeypatch.setattr(cc1, "START_RETRY_DELAY", 0)
    monkeypatch.setattr(cc1, "START_ACK_TIMEOUT", 0.05)
    sent = []
    queue = list(acks)

    async def fake_send(cmd, data):
        sent.append((cmd, data))
        ack = queue.pop(0) if queue else 0
        if cmd == 128 and ack is not None:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(printer._handle_message(
                    json.dumps({"Data": {"Cmd": 128, "Data": {"Ack": ack}}}))))
        return True
    monkeypatch.setattr(printer, "send_cmd", fake_send)
    return sent


@pytest.mark.asyncio
async def test_start_right_after_upload_retries_until_the_file_is_there(printer, monkeypatch):
    # Seen on a real CC1: start sent the moment the upload finished got Ack 2
    # ("file not found"), though the file was listed on the printer a moment later.
    import state
    b = _Broadcasts()
    monkeypatch.setattr(state, "broadcast_to_browsers", b)
    sent = _start_acks(printer, monkeypatch, [2, 2, 0])
    assert await printer.start_print_file("a.gcode", print_opts={"leveling": True}) is True
    assert [c for c, _ in sent] == [128, 128, 128]
    assert not [m for m in b.sent if m.get("type") == "error"]


@pytest.mark.asyncio
async def test_start_that_stays_rejected_returns_false_and_reports_once(printer, monkeypatch):
    import state
    import printers.cc1 as cc1
    b = _Broadcasts()
    monkeypatch.setattr(state, "broadcast_to_browsers", b)
    sent = _start_acks(printer, monkeypatch, [2] * 20)
    assert await printer.start_print_file("a.gcode") is False
    assert len(sent) == cc1.START_RETRIES + 1
    errors = [m for m in b.sent if m.get("type") == "error"]
    assert len(errors) == 1 and "wasn't found" in errors[0]["message"]


@pytest.mark.asyncio
async def test_other_rejections_are_not_retried(printer, monkeypatch):
    import state
    monkeypatch.setattr(state, "broadcast_to_browsers", _Broadcasts())
    sent = _start_acks(printer, monkeypatch, [1])
    assert await printer.start_print_file("a.gcode") is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_start_without_an_answer_counts_as_sent(printer, monkeypatch):
    import state
    monkeypatch.setattr(state, "broadcast_to_browsers", _Broadcasts())
    sent = _start_acks(printer, monkeypatch, [None])
    assert await printer.start_print_file("a.gcode") is True
    assert len(sent) == 1
