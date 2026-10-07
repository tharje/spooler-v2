"""
printers/base.py — print-start/print-end transition handling.

These exercise PrinterConnection directly (not a protocol subclass): connect()
and send_cmd() are never called, only the status-transition machinery, so the
abstract methods raising NotImplementedError never matters here.
"""

import time

import pytest

import persistence
import printers.base as base_mod
from printers.base import PrinterConnection, classify_display_state, classify_print_transition


# ── classify_print_transition (pure function) ───────────────────────────────

@pytest.mark.parametrize("prev,cur,expected", [
    (None, 0, None),            # unknown -> idle: nothing happened
    (None, 2, "start"),         # unknown -> printing: start
    (0, 2, "start"),            # idle -> printing: start
    (2, 2, None),                # printing -> printing: no re-trigger
    (2, 9, "end"),               # printing -> complete: end
    (2, 8, "end"),               # printing -> cancelled: end
    (2, 14, "end"),              # printing -> error: end
    (2, 0, "end"),                # printing -> idle (CC2-style completion): end
    (9, 2, "start"),             # complete -> printing directly: must still be "start"
                                  # (regression test for the ACTIVE-set bug: 9 used to be
                                  # considered "active", so this transition was missed)
    (9, 9, None),                 # sitting at complete: nothing happens repeatedly
    (6, 9, "end"),                 # paused -> complete: end
    (6, 2, None),                  # resume: paused -> printing must NOT be "start"
                                    # (regression test: paused/pausing were never in
                                    # ACTIVE_STATUSES, so every resume used to incorrectly
                                    # satisfy "prev not in ACTIVE" and fire "start",
                                    # resetting per-print extrusion tracking)
    (5, 2, None),                  # resume from "pausing": also not a new print
    (6, 14, "end"),                 # error while paused: end
])
def test_classify_print_transition(prev, cur, expected):
    assert classify_print_transition(prev, cur) == expected


# ── classify_display_state (pure function) ───────────────────────────────────

@pytest.mark.parametrize("connected,code,homing,expected", [
    (False, 2, False, "offline"),       # disconnected always wins, regardless of code
    (True, None, False, "idle"),
    (True, 0, False, "idle"),
    (True, 1, False, "preparing"),
    (True, 2, False, "printing"),
    (True, 5, False, "pausing"),
    (True, 6, False, "paused"),
    (True, 7, False, "stopping"),
    (True, 8, False, "cancelled"),
    (True, 9, False, "complete"),
    (True, 14, False, "error"),         # the confirmed bug: must NOT be "cancelled"
    (True, 0, True, "preparing"),       # CC1's CurrentStatus[0]==9 + Status==0 quirk
    (True, 11, False, "preparing"),     # CC1: printer checking (Elegoo SDK)
    (True, 17, False, "preparing"),     # CC1: resonance test
    (True, 22, False, "preparing"),
    (True, 23, False, "preparing"),     # CC1: filament feeding from the screen
    (True, 27, False, "unknown"),       # undocumented code: must NOT be "idle"
    (True, 99, False, "unknown"),
])
def test_classify_display_state(connected, code, homing, expected):
    assert classify_display_state(connected, code, homing) == expected


def test_classify_display_state_logs_unknown_code_only_once(capsys):
    base_mod._logged_unknown_display_codes.discard(99)
    classify_display_state(True, 99)
    classify_display_state(True, 99)
    classify_display_state(True, 99)
    out = capsys.readouterr().out
    assert out.count("99") == 1


# ── _check_print_transition (integration of the above into PrinterConnection) ─

@pytest.fixture
def printer(monkeypatch):
    p = PrinterConnection("pid1", "10.0.0.5", "Test Printer")
    p.connected = True
    monkeypatch.setattr(base_mod, "get_spool_density", lambda printer_id: 1.24)
    monkeypatch.setattr(base_mod, "spoolman_deduct", lambda *a, **kw: None)
    monkeypatch.setattr(base_mod, "spoolman_deduct_spool", lambda *a, **kw: None)
    return p


def _set_status(printer, status_code, **printinfo_overrides):
    pi = {"Status": status_code, **printinfo_overrides}
    printer.status = {"PrintInfo": pi}


@pytest.mark.asyncio
async def test_print_start_initializes_extrusion_tracking(printer):
    printer._extrusion_snapshot = 999.0
    printer._spool_extrusion = {1: 50.0}
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()
    assert printer._extrusion_snapshot == 0.0
    assert printer._spool_extrusion == {}
    assert printer._print_start_time is not None


@pytest.mark.asyncio
async def test_complete_to_printing_resets_snapshot_regression(printer):
    """Regression test for the ACTIVE-set bug: complete (9) directly to
    printing (2), with no idle poll observed in between, must still reset
    per-print extrusion tracking — this was the actual production bug."""
    _set_status(printer, 9, TotalExtrusion=500)
    await printer._check_print_transition()  # printer sits at "complete"
    printer._extrusion_snapshot = 500.0        # simulate stale leftover snapshot
    printer._spool_extrusion = {1: 500.0}

    _set_status(printer, 2, TotalExtrusion=0)  # next print starts directly from 9
    await printer._check_print_transition()

    assert printer._extrusion_snapshot == 0.0
    assert printer._spool_extrusion == {}


@pytest.mark.asyncio
async def test_print_complete_records_history_entry(printer):
    _set_status(printer, 2, TotalExtrusion=0, Filename="a.gcode")
    await printer._check_print_transition()

    _set_status(printer, 9, TotalExtrusion=1000, Filename="a.gcode", PrintTime=600)
    await printer._check_print_transition()

    history = persistence.load_history()
    assert len(history) == 1
    assert history[0]["filename"] == "a.gcode"
    assert history[0]["completed"] is True
    assert history[0]["print_time_s"] == 600


@pytest.mark.asyncio
async def test_print_cancelled_records_history_with_completed_false(printer):
    _set_status(printer, 2, TotalExtrusion=0, Filename="b.gcode")
    await printer._check_print_transition()

    _set_status(printer, 8, TotalExtrusion=300, Filename="b.gcode", PrintTime=120)
    await printer._check_print_transition()

    history = persistence.load_history()
    assert len(history) == 1
    assert history[0]["completed"] is False


@pytest.mark.asyncio
async def test_no_history_entry_when_no_filament_and_no_filename(printer):
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()

    _set_status(printer, 9, TotalExtrusion=0)  # nothing was ever printed
    await printer._check_print_transition()

    assert persistence.load_history() == []


@pytest.mark.asyncio
async def test_idle_to_idle_does_not_append_history(printer):
    _set_status(printer, 0, TotalExtrusion=0)
    await printer._check_print_transition()
    _set_status(printer, 0, TotalExtrusion=0)
    await printer._check_print_transition()
    assert persistence.load_history() == []


# ── state_reason lifecycle ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pause_with_unknown_source_has_no_reason_info(printer):
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()
    _set_status(printer, 6, TotalExtrusion=0)  # paused, not caused by Spooler, no hint
    await printer._check_print_transition()

    assert printer.state_reason["kind"] == "pause"
    assert printer.state_reason["initiated_by"] == "unknown"
    assert printer.state_reason["category"] == "unknown"


@pytest.mark.asyncio
async def test_pause_from_spooler_is_attributed_correctly(printer):
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()
    printer.mark_spooler_command()  # simulates ws_handler sending the pause command
    _set_status(printer, 6, TotalExtrusion=0)
    await printer._check_print_transition()

    assert printer.state_reason["kind"] == "pause"
    assert printer.state_reason["initiated_by"] == "spooler"


@pytest.mark.asyncio
async def test_spooler_attribution_expires_after_15_seconds(printer, monkeypatch):
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()
    printer.mark_spooler_command()
    printer._last_spooler_cmd_at -= 16  # simulate 16s having passed
    _set_status(printer, 6, TotalExtrusion=0)
    await printer._check_print_transition()

    assert printer.state_reason["initiated_by"] == "unknown"


@pytest.mark.asyncio
async def test_pausing_then_paused_keeps_same_reason(printer):
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()
    printer.mark_spooler_command()
    _set_status(printer, 5, TotalExtrusion=0)  # pausing
    await printer._check_print_transition()
    first_since = printer.state_reason["since"]

    _set_status(printer, 6, TotalExtrusion=0)  # now paused
    await printer._check_print_transition()

    assert printer.state_reason["kind"] == "pause"
    assert printer.state_reason["since"] == first_since  # not reset


@pytest.mark.asyncio
async def test_resume_clears_state_reason_and_records_pause_duration(printer):
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()
    _set_status(printer, 6, TotalExtrusion=0)  # pause begins
    await printer._check_print_transition()
    assert len(printer._current_print_pauses) == 1
    assert printer._current_print_pauses[0]["until"] is None

    _set_status(printer, 2, TotalExtrusion=0)  # resume
    await printer._check_print_transition()

    assert printer.state_reason is None
    assert printer._current_print_pauses[0]["until"] is not None
    assert printer._current_print_pauses[0]["duration_s"] is not None


@pytest.mark.asyncio
async def test_state_reason_persists_through_idle_until_next_print_start(printer):
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()
    _set_status(printer, 14, TotalExtrusion=0)  # error
    await printer._check_print_transition()
    assert printer.state_reason["kind"] == "error"

    _set_status(printer, 0, TotalExtrusion=0)  # back to idle — reason must stay
    await printer._check_print_transition()
    assert printer.state_reason is not None
    assert printer.state_reason["kind"] == "error"

    _set_status(printer, 2, TotalExtrusion=0)  # a genuinely new print starts
    await printer._check_print_transition()
    assert printer.state_reason is None


@pytest.mark.asyncio
async def test_error_end_state_sets_history_fields(printer):
    _set_status(printer, 2, TotalExtrusion=0, Filename="c.gcode")
    await printer._check_print_transition()
    printer.state_reason = {
        "kind": "error", "initiated_by": "printer", "code": "42",
        "category": "unknown", "message": "Nozzle thermal runaway",
        "raw": {}, "since": "2026-01-01T10:00:00",
    }
    _set_status(printer, 14, TotalExtrusion=400, Filename="c.gcode", PrintTime=90)
    await printer._check_print_transition()

    history = persistence.load_history()
    assert len(history) == 1
    entry = history[0]
    assert entry["end_state"] == "error"
    assert entry["completed"] is False
    assert entry["error_code"] == "42"
    assert entry["error_message"] == "Nozzle thermal runaway"
    assert entry["initiated_by"] == "printer"
    assert "id" in entry and entry["id"]


@pytest.mark.asyncio
async def test_cancelled_end_state_has_no_error_fields(printer):
    _set_status(printer, 2, TotalExtrusion=0, Filename="d.gcode")
    await printer._check_print_transition()
    printer.mark_spooler_command()
    _set_status(printer, 8, TotalExtrusion=200, Filename="d.gcode", PrintTime=30)
    await printer._check_print_transition()

    history = persistence.load_history()
    entry = history[0]
    assert entry["end_state"] == "cancelled"
    assert entry["error_code"] is None
    assert entry["error_message"] is None
    assert entry["initiated_by"] == "spooler"


# ── last_seen (T4) ────────────────────────────────────────────────────────────

def test_mark_seen_records_the_time(printer):
    assert printer.last_seen is None
    printer._mark_seen()
    assert printer.last_seen is not None
    assert printer.last_seen <= time.time()


@pytest.mark.asyncio
async def test_broadcast_state_never_touches_last_seen(printer):
    # last_seen moved out of _broadcast_state (I2): broadcasting also happens
    # for browser-side reasons and says nothing about the printer.
    _set_status(printer, 0)
    await printer._broadcast_state()
    assert printer.last_seen is None
    printer.last_seen = 1000.0
    await printer._broadcast_state()
    printer.connected = False
    await printer._broadcast_state()       # a disconnect notice
    assert printer.last_seen == 1000.0


def test_to_dict_exposes_last_seen(printer):
    printer.last_seen = 12345.0
    assert printer.to_dict()["last_seen"] == 12345.0
