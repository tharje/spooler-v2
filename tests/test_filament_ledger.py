"""T22: filament deductions that survive Spoolman being down, repeated end
messages, restarts, counters that go backwards and spool changes."""

import asyncio
import json

import pytest

import features
import ledger
import persistence
import printers.base as base
import printaccount
import state
from printaccount import PrintAccounting
from printers.base import PrinterConnection

NOW = 1_760_000_000


class FakeSpoolman:
    """Stands in for Spoolman: records what it actually applied."""
    def __init__(self):
        self.applied = []
        self.script = []          # outcomes to hand out first, then "ok"
        self.calls = 0

    def use(self, spool_id, grams):
        self.calls += 1
        out = self.script.pop(0) if self.script else {"outcome": "ok", "result": {"id": spool_id}}
        if out["outcome"] == "ok":
            self.applied.append((spool_id, grams))
        return out


def never(printer_id):
    return ("none", None)


@pytest.fixture
def sm():
    return FakeSpoolman()


def add(spool=12, grams=41.3, hid="h1", **kw):
    return ledger.add(hid, "p1", spool, grams, grams * 330, now=NOW, **kw)


# ── delivery ─────────────────────────────────────────────────────────────────

def test_spoolman_down_then_up_sends_exactly_once(sm):
    add()
    sm.script = [{"outcome": "error", "message": "Could not reach Spoolman (ConnectionRefusedError)"}]
    assert ledger.process_due(sm.use, never, now=NOW) == 1
    e = ledger.get("h1:12")
    assert e["status"] == "failed" and e["attempts"] == 1 and "Spoolman" in e["last_error"]
    assert sm.applied == []
    assert ledger.process_due(sm.use, never, now=NOW + 5) == 0           # backing off: not due yet
    assert ledger.process_due(sm.use, never, now=NOW + 31) == 1
    assert ledger.get("h1:12")["status"] == "sent" and ledger.get("h1:12")["sent_at"]
    assert sm.applied == [(12, 41.3)]
    assert ledger.process_due(sm.use, never, now=NOW + 99999) == 0       # sent entries are never sent again
    assert sm.calls == 2 and len(sm.applied) == 1


def test_backoff_grows_then_settles_at_an_hour(sm):
    add()
    waits = []
    t = NOW
    for _ in range(6):
        sm.script = [{"outcome": "error", "message": "down"}]
        ledger.process_due(sm.use, never, now=t)
        e = ledger.get("h1:12")
        waits.append(e["next_try_at"] - t)
        t = e["next_try_at"]
    assert waits == [30, 120, 600, 3600, 3600, 3600]


def test_same_print_and_spool_can_only_be_recorded_once(sm):
    assert add() is True
    assert add() is False                           # a repeated end message
    assert add(spool=13) is True                    # another spool of the same print is fine
    ledger.process_due(sm.use, never, now=NOW)
    assert len(sm.applied) == 2 and ledger.summary() == {"unsent": 0, "attention": 0}


def test_pending_entry_is_sent_after_a_restart(sm):
    add()
    assert ledger.recover_interrupted() == 0        # "pending" is not "interrupted"
    ledger.process_due(sm.use, never, now=NOW + 1)  # what the sender does after startup
    assert sm.applied == [(12, 41.3)]


def test_spool_deleted_in_spoolman_fails_readably_without_fast_retries(sm):
    add()
    sm.script = [{"outcome": "not_found", "message": "Spoolman has no spool with id 12 (was it deleted?)"}]
    ledger.process_due(sm.use, never, now=NOW)
    e = ledger.get("h1:12")
    assert e["status"] == "failed" and "no spool with id 12" in e["last_error"]
    assert e["next_try_at"] - NOW >= 3600
    assert ledger.process_due(sm.use, never, now=NOW + 600) == 0
    assert sm.calls == 1


def test_unknown_outcome_waits_for_a_person_and_is_never_resent(sm):
    add()
    sm.script = [{"outcome": "uncertain", "message": "Spoolman did not answer after the request was sent"}]
    ledger.process_due(sm.use, never, now=NOW)
    e = ledger.get("h1:12")
    assert e["status"] == "failed" and e["next_try_at"] is None
    assert ledger.process_due(sm.use, never, now=NOW + 10 ** 7) == 0
    assert sm.calls == 1 and ledger.summary()["attention"] == 1
    assert ledger.retry_now("h1:12")                # the person decides
    ledger.process_due(sm.use, never, now=NOW + 10 ** 7)
    assert sm.applied == [(12, 41.3)]


def test_entry_interrupted_mid_send_is_held_not_resent_after_restart(sm):
    add()
    assert ledger.mark_sending("h1:12")             # ... Spooler dies here
    assert ledger.mark_sending("h1:12") is None     # nobody else can claim it meanwhile
    assert ledger.recover_interrupted() == 1
    e = ledger.get("h1:12")
    assert e["status"] == "failed" and e["next_try_at"] is None and "not known whether Spoolman" in e["last_error"]
    assert ledger.process_due(sm.use, never, now=NOW + 10 ** 7) == 0 and sm.calls == 0


def test_retry_and_discard(sm):
    add()
    sm.script = [{"outcome": "error", "message": "down"}]
    ledger.process_due(sm.use, never, now=NOW)
    assert ledger.retry_now("h1:12") and ledger.get("h1:12")["next_try_at"] == 0
    assert ledger.discard("h1:12", "Discarded by the user")
    assert ledger.get("h1:12")["status"] == "discarded"
    assert not ledger.discard("h1:12", "again") and not ledger.retry_now("h1:12")
    assert ledger.process_due(sm.use, never, now=NOW + 10 ** 7) == 0 and sm.applied == []


def test_summary_flags_entries_needing_attention(sm):
    add()
    for i in range(ledger.ATTENTION_AFTER):
        sm.script = [{"outcome": "error", "message": "down"}]
        ledger.process_due(sm.use, never, now=NOW + i * 4000)
    assert ledger.summary() == {"unsent": 1, "attention": 1}


def test_unknown_spool_uses_the_printers_spool_at_send_time(sm):
    add(spool=None, location="CC2")
    assert ledger.process_due(sm.use, lambda pid: ("unreachable", None), now=NOW) == 1
    assert ledger.get("h1:none")["status"] == "failed" and sm.calls == 0
    assert ledger.process_due(sm.use, lambda pid: ("found", 77), now=NOW + 31) == 1
    assert sm.applied == [(77, 41.3)] and ledger.get("h1:none")["spool_id"] == 77
    add(spool=None, hid="h2")
    ledger.process_due(sm.use, never, now=NOW)
    e = ledger.get("h2:none")
    assert e["status"] == "failed" and "No spool is assigned" in e["last_error"] and e["next_try_at"] - NOW >= 3600


def test_a_discarded_entry_can_be_dismissed_from_the_list(sm):
    add()
    assert not ledger.dismiss("h1:12")                    # still waiting: dismissing is not allowed, discard first
    assert ledger.discard("h1:12", "x") and ledger.dismiss("h1:12")
    assert ledger.get("h1:12")["status"] == "dismissed" and not ledger.dismiss("h1:12")
    assert ledger.process_due(sm.use, never, now=NOW + 10 ** 7) == 0 and sm.applied == []


def test_old_sent_and_discarded_entries_are_forgotten_after_90_days(sm):
    add(); add(hid="h2"); add(hid="h3")
    ledger.process_due(sm.use, never, now=NOW)                  # all three sent
    ledger.discard("h2:12", "x") or None
    assert ledger.cleanup(now=NOW + 89 * 86400) == 0
    assert ledger.cleanup(now=NOW + 91 * 86400) == 3
    assert ledger.all_entries() == []


def test_unsent_entries_are_never_forgotten():
    add()
    assert ledger.cleanup(now=NOW + 500 * 86400) == 0 and len(ledger.unsent()) == 1


def test_ledger_file_is_private(tmp_path):
    add()
    assert (tmp_path / "spoolman_ledger.json").stat().st_mode & 0o077 == 0


def test_after_send_hook_gets_the_spoolman_reply(sm):
    add()
    seen = []
    ledger.process_due(sm.use, never, on_sent=lambda e, r: seen.append((e["spool_id"], r)), now=NOW)
    assert seen == [(12, {"id": 12})]


# ── per-print accounting ─────────────────────────────────────────────────────

def test_counter_going_backwards_is_a_reset_not_negative_use():
    a = PrintAccounting(current_spool=1)
    a.observe(100.0)
    assert a.observe(20.0) is True                  # printer counter reset
    a.observe(50.0)
    assert a.total_mm() == 150.0 and a.finish() == {1: 150.0}


def test_small_dips_in_an_estimated_figure_are_noise():
    a = PrintAccounting(current_spool=1)
    a.observe(1000.0)
    assert a.observe(990.0) is False                # CC2's figure is an estimate
    assert a.total_mm() == 1000.0
    a.observe(1200.0)
    assert a.finish() == {1: 1200.0}


def test_spool_change_splits_usage_at_the_moment_of_the_change():
    a = PrintAccounting(current_spool=1)
    a.observe(100.0); a.switch_spool(2)
    a.observe(250.0); a.switch_spool(1)
    a.observe(300.0)
    assert a.finish() == {1: 150.0, 2: 150.0}       # 0-100 and 250-300 on spool 1; 100-250 on spool 2
    b = PrintAccounting(current_spool=1)
    b.observe(80.0); b.switch_spool(1)              # "changing" to the same spool changes nothing
    assert b.finish() == {1: 80.0}


def test_accounting_round_trips_through_the_state_file():
    a = PrintAccounting(current_spool=None, start_spool=9, filename="x.gcode", started_at=NOW)
    a.observe(100.0); a.switch_spool(3); a.observe(40.0)
    printaccount.save_active("p1", a)
    b = printaccount.load_active("p1")
    assert (b.print_id, b.current_spool, b.start_spool, b.filename) == (a.print_id, 3, 9, "x.gcode")
    assert b.total_mm() == a.total_mm() and b.per_spool == {None: 100.0}
    printaccount.clear_active("p1")
    assert printaccount.load_active("p1") is None and printaccount.load_active("nobody") is None


# ── the printer object ───────────────────────────────────────────────────────

@pytest.fixture
def make_printer(monkeypatch):
    sent = []

    async def broadcast(msg):
        sent.append(msg)
    monkeypatch.setattr(state, "broadcast_to_browsers", broadcast)
    monkeypatch.setattr(base, "get_spool_density", lambda pid: 1.24)
    monkeypatch.setattr(state, "tray_map", {"pid1": {"0": 1, "1": 2}})

    def make():
        p = PrinterConnection("pid1", "10.0.0.5", "Bench")
        p.connected = True
        p.emitted = []
        p._emit = lambda event, *a, **k: p.emitted.append(event)

        async def noop(on):
            p.light.append(on)
        p.light = []
        p._auto_light = noop

        async def nopic(entry_id):
            pass
        p._save_print_picture = nopic
        return p
    make.sent = sent
    return make


def status(p, code, extrusion=0, tray=None, **extra):
    s = {"PrintInfo": {"Status": code, "TotalExtrusion": extrusion, "Filename": "a.gcode", **extra}}
    if tray is not None:
        s["canvas_info"] = {"active_tray_id": tray, "canvas_list": [{"canvas_id": 0}]}
    p.status = s


def entries_by_spool():
    return {e["spool_id"]: e for e in ledger.all_entries()}


@pytest.mark.asyncio
async def test_print_with_a_spool_change_is_split_between_the_spools(make_printer):
    p = make_printer()
    status(p, 0, tray=0); await p._check_print_transition()
    status(p, 13, 0, tray=0); await p._check_print_transition()
    status(p, 13, 3300, tray=0); await p._check_print_transition()
    status(p, 13, 3300, tray=1); p._on_tray_change(2)                 # the change
    status(p, 13, 9900, tray=1); await p._check_print_transition()
    status(p, 9, 9900, tray=1); await p._check_print_transition()
    e = entries_by_spool()
    assert set(e) == {1, 2}
    assert e[1]["mm"] == 3300 and e[2]["mm"] == 6600
    assert e[1]["id"].endswith(":1") and e[1]["history_id"] == e[2]["history_id"]
    h = persistence.load_history()[0]
    assert h["id"] == e[1]["history_id"]


@pytest.mark.asyncio
async def test_single_material_print_queues_one_deduction_for_the_spool_at_the_printer(make_printer, monkeypatch):
    monkeypatch.setattr(base, "last_spool_info", lambda pid: {"status": "found", "spool_id": 5})
    p = make_printer()
    status(p, 0); await p._check_print_transition()
    status(p, 13, 0); await p._check_print_transition()
    status(p, 9, 4000); await p._check_print_transition()
    [e] = ledger.all_entries()
    assert e["spool_id"] == 5 and e["status"] == "pending" and e["mm"] == 4000


@pytest.mark.asyncio
async def test_spoolman_unreachable_at_the_end_still_queues_the_deduction(make_printer, monkeypatch):
    monkeypatch.setattr(base, "last_spool_info", lambda pid: {"status": "unreachable"})
    p = make_printer()
    status(p, 0); await p._check_print_transition()
    status(p, 13, 0); await p._check_print_transition()
    status(p, 9, 4000); await p._check_print_transition()
    [e] = ledger.all_entries()
    assert e["spool_id"] is None and e["status"] == "pending" and e["location"] == "Bench"


@pytest.mark.asyncio
async def test_spool_seen_at_the_start_is_the_target_when_spoolman_is_down_at_the_end(make_printer, monkeypatch):
    info = {"status": "found", "spool_id": 8}
    monkeypatch.setattr(base, "last_spool_info", lambda pid: dict(info))
    p = make_printer()
    status(p, 0); await p._check_print_transition()
    status(p, 13, 0); await p._check_print_transition()
    await asyncio.sleep(0.05)                       # the start-of-print lookup
    assert p._acct.start_spool == 8
    info.clear(); info["status"] = "unreachable"
    status(p, 9, 4000); await p._check_print_transition()
    assert ledger.all_entries()[0]["spool_id"] == 8


@pytest.mark.asyncio
async def test_unlinked_slot_is_recorded_as_discarded_with_the_reason(make_printer):
    p = make_printer()
    status(p, 0, tray=3); await p._check_print_transition()
    status(p, 13, 0, tray=3); await p._check_print_transition()
    status(p, 9, 1000, tray=3); await p._check_print_transition()
    [e] = ledger.all_entries()
    assert e["spool_id"] is None and e["status"] == "discarded" and "No spool is linked" in e["last_error"]
    assert ledger.summary()["unsent"] == 0


@pytest.mark.asyncio
async def test_spoolman_switched_off_creates_no_entries_but_keeps_old_ones(make_printer):
    add()
    features.set_enabled("spoolman", False)
    p = make_printer()
    status(p, 0, tray=0); await p._check_print_transition()
    status(p, 13, 0, tray=0); await p._check_print_transition()
    status(p, 9, 1000, tray=0); await p._check_print_transition()
    assert [e["id"] for e in ledger.all_entries()] == ["h1:12"]


@pytest.mark.asyncio
async def test_a_repeated_end_message_gives_no_second_deduction(make_printer):
    p = make_printer()
    status(p, 0, tray=0); await p._check_print_transition()
    status(p, 13, 0, tray=0); await p._check_print_transition()
    acct = p._acct
    status(p, 9, 2000, tray=0); await p._check_print_transition()
    await p._check_print_transition()               # polling and push both delivering "complete"
    assert len(ledger.all_entries()) == 1
    # and even if the same record were written again, the id refuses it
    assert not ledger.add(acct.print_id, "pid1", 1, 6.5, 2000)


@pytest.mark.asyncio
async def test_restart_mid_print_neither_loses_nor_repeats_filament(make_printer):
    p = make_printer()
    status(p, 0, tray=0); await p._check_print_transition()
    status(p, 13, 0, tray=0); await p._check_print_transition()
    status(p, 13, 100, tray=0); await p._check_print_transition()
    status(p, 13, 100, tray=1); p._on_tray_change(2)              # a swap, 100 mm on spool 1
    status(p, 13, 150, tray=1); await p._check_print_transition()
    p._save_accounting(force=True)
    print_id = p._acct.print_id

    q = make_printer()                                            # --- Spooler restarts ---
    status(q, 13, 150, tray=1); await q._check_print_transition()
    assert q._acct.print_id == print_id and q._acct.current_spool == 2
    assert q.emitted == [] and q.light == []                      # not announced as a new print
    status(q, 13, 300, tray=1); await q._check_print_transition()
    status(q, 9, 300, tray=1); await q._check_print_transition()
    e = entries_by_spool()
    assert e[1]["mm"] == 100 and e[2]["mm"] == 200                # 300 in total, nothing twice
    assert persistence.load_history()[0]["id"] == print_id


@pytest.mark.asyncio
async def test_restart_mid_print_with_no_saved_state_adopts_the_print_quietly(make_printer):
    q = make_printer()
    status(q, 13, 400, tray=0); await q._check_print_transition()
    assert q.emitted == [] and q.light == [] and q._print_start_time is None
    status(q, 9, 900, tray=0); await q._check_print_transition()
    [e] = ledger.all_entries()
    assert e["spool_id"] == 1 and e["mm"] == 900                  # the printer's per-print total, as before
    assert persistence.load_history()[0]["started_at"] is None


@pytest.mark.asyncio
async def test_print_that_ended_while_spooler_was_off_is_not_lost(make_printer):
    p = make_printer()
    a = PrintAccounting(current_spool=1, filename="a.gcode")
    a.observe(500.0)
    printaccount.save_active("pid1", a)
    status(p, 0, tray=0); await p._check_print_transition()      # first thing seen after restart: idle
    [e] = ledger.all_entries()
    assert e["spool_id"] == 1 and e["mm"] == 500 and "not running" in e["note"]
    assert printaccount.load_active("pid1") is None


@pytest.mark.asyncio
async def test_counter_reset_in_a_running_print_adds_up(make_printer):
    p = make_printer()
    status(p, 0, tray=0); await p._check_print_transition()
    status(p, 13, 0, tray=0); await p._check_print_transition()
    status(p, 13, 800, tray=0); await p._check_print_transition()
    status(p, 13, 50, tray=0); await p._check_print_transition()   # counter dropped
    status(p, 9, 250, tray=0); await p._check_print_transition()
    [e] = ledger.all_entries()
    assert e["mm"] == 1050                                          # 800 before the reset + the new count (250), never negative
    assert persistence.load_history()[0]["filament_mm"] == 1050     # the history agrees


@pytest.mark.asyncio
async def test_active_state_file_is_cleared_when_the_print_ends(make_printer):
    p = make_printer()
    status(p, 0, tray=0); await p._check_print_transition()
    status(p, 13, 0, tray=0); await p._check_print_transition()
    assert printaccount.load_active("pid1") is not None
    status(p, 9, 100, tray=0); await p._check_print_transition()
    assert printaccount.load_active("pid1") is None and p._acct is None


# ── HTTP ─────────────────────────────────────────────────────────────────────

class _Req:
    def __init__(self, path):
        self.path, self.out = path, None

    def _read_body(self):
        return b""

    def _json(self, data, code=200):
        self.out = (code, data)

    def _ledger_changed(self):
        pass


def test_ledger_http_actions_and_gating(sm):
    import http_handler
    add()
    sm.script = [{"outcome": "error", "message": "down"}]
    ledger.process_due(sm.use, never, now=NOW)
    r = _Req("/api/spoolman-ledger")
    http_handler.SPHandler._handle_ledger_list(r)
    assert r.out[0] == 200 and r.out[1]["summary"]["unsent"] == 1 and r.out[1]["items"][0]["id"] == "h1:12"

    r = _Req("/api/spoolman-ledger/h1:12/retry")
    http_handler.SPHandler._handle_ledger_action(r, "retry")
    assert r.out == (200, {"ok": True}) and ledger.get("h1:12")["next_try_at"] == 0
    r = _Req("/api/spoolman-ledger/h1:12")
    http_handler.SPHandler._handle_ledger_action(r, "discard")
    assert r.out == (200, {"ok": True}) and ledger.get("h1:12")["status"] == "discarded"
    r = _Req("/api/spoolman-ledger/nope")
    http_handler.SPHandler._handle_ledger_action(r, "discard")
    assert r.out[0] == 404
    assert http_handler.SPHandler._handle_ledger_list._feature_gate == "spoolman"
    assert http_handler.SPHandler._handle_ledger_action._feature_gate == "spoolman"


@pytest.mark.asyncio
async def test_print_that_finished_while_spooler_was_off_uses_the_printers_final_figure(make_printer):
    p = make_printer()
    a = PrintAccounting(current_spool=1, filename="a.gcode")
    a.observe(500.0)                                              # all that was saved before the stop
    printaccount.save_active("pid1", a)
    status(p, 9, 1800, tray=0); await p._check_print_transition() # restarted after it completed: printer still says 1800
    [e] = ledger.all_entries()
    assert e["mm"] == 1800


@pytest.mark.asyncio
async def test_a_different_file_running_after_restart_flushes_the_old_print_and_starts_fresh(make_printer):
    p = make_printer()
    a = PrintAccounting(current_spool=1, filename="old.gcode")
    a.observe(500.0)
    printaccount.save_active("pid1", a)
    status(p, 13, 40, tray=0); await p._check_print_transition()  # a.gcode is running now
    [e] = ledger.all_entries()
    assert e["mm"] == 500 and "not running" in e["note"]          # the old print still counted
    assert p._acct.print_id != a.print_id and p._acct.filename == "a.gcode"


@pytest.mark.asyncio
async def test_missing_filename_after_restart_still_resumes_the_saved_print(make_printer):
    a = PrintAccounting(current_spool=1, filename="a.gcode")
    a.observe(500.0)
    printaccount.save_active("pid1", a)
    q = make_printer()
    q.status = {"PrintInfo": {"Status": 13, "TotalExtrusion": 600, "Filename": ""}}
    await q._check_print_transition()
    assert q._acct.print_id == a.print_id and q._acct.total_mm() == 600


@pytest.mark.asyncio
async def test_shutdown_saves_the_running_prints_figures(make_printer):
    p = make_printer()
    status(p, 0, tray=0); await p._check_print_transition()
    status(p, 13, 0, tray=0); await p._check_print_transition()
    status(p, 13, 30, tray=0); await p._check_print_transition()   # too little for a throttled save
    status(p, 13, 45, tray=0)
    p.save_accounting_now()
    assert printaccount.load_active("pid1").total_mm() == 45


@pytest.mark.asyncio
async def test_print_that_ends_from_a_busy_status_is_still_recorded(make_printer):
    """The printer's last status before idle was not 'printing' (e.g. 15 'preparing'): the print still ended."""
    p = make_printer()
    status(p, 0, tray=0); await p._check_print_transition()
    status(p, 13, 0, tray=0); await p._check_print_transition()
    status(p, 13, 700, tray=0); await p._check_print_transition()
    status(p, 15, 0, tray=0); await p._check_print_transition()     # busy, counter shows 0
    status(p, 0, 0, tray=0); await p._check_print_transition()      # idle
    [e] = ledger.all_entries()
    assert e["mm"] == 700
    assert persistence.load_history()[0]["filament_mm"] == 700
    assert printaccount.load_active("pid1") is None
