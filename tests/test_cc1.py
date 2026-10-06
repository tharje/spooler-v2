"""printers/cc1.py -- CC1 status phases, command rejections, reason hint."""

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
    assert set(CC1_ERROR_CODES) == {101, 102, 103, 104, 304, 502, 701, 702, 703}
    assert lookup(502, "cc1")["category"] == "leveling"
    assert lookup(502, "cc2") is None      # 502 isn't a CC2 code
    assert lookup(704, "cc1") is None
