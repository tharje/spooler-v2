"""I2: last_seen means "the printer itself said something", and the browser
compares it against the server's clock, not its own."""

import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import persistence
import state
import ws_handler
from printers.base import PrinterConnection
from printers.cc1 import CC1Connection
from printers.cc2 import CC2Connection


@pytest.fixture(autouse=True)
def quiet_browsers(monkeypatch):
    async def nobody(msg):
        pass
    monkeypatch.setattr(state, "broadcast_to_browsers", nobody)


def test_to_dict_carries_the_servers_clock():
    p = PrinterConnection("a", "10.0.0.1", "A")
    before = time.time()
    d = p.to_dict()
    assert before <= d["server_time"] <= time.time()


@pytest.mark.asyncio
async def test_broadcasting_state_does_not_count_as_hearing_from_the_printer():
    p = PrinterConnection("a", "10.0.0.1", "A")
    p.connected = True
    await p._broadcast_state()
    assert p.last_seen is None
    p._mark_seen()
    assert p.last_seen is not None


@pytest.mark.asyncio
async def test_renaming_or_readdressing_a_printer_leaves_last_seen_alone(monkeypatch):
    p = PrinterConnection("a", "10.0.0.1", "A")
    p.connected = True
    p.last_seen = 1000.0
    monkeypatch.setattr(state, "printers", {"a": p})
    monkeypatch.setattr(p, "start", lambda: asyncio.sleep(0))      # no real reconnect
    sent = []

    class WS:
        async def send(self, data):
            sent.append(data)
    await ws_handler.handle_browser_message(
        WS(), json.dumps({"action": "update_printer", "printer_id": "a", "name": "Renamed", "ip": "10.0.0.2"}))
    assert p.name == "Renamed" and p.ip == "10.0.0.2"
    assert p.last_seen == 1000.0


@pytest.mark.asyncio
async def test_cc1_message_marks_seen():
    p = CC1Connection("a", "10.0.0.1", "A")
    await p._handle_message(json.dumps({"Status": {"PrintInfo": {"Status": 0}}}))
    assert p.last_seen is not None


class _Msg:
    def __init__(self, topic, data=None):
        self.topic = topic
        self.payload = json.dumps(data or {}).encode()


@pytest.mark.asyncio
async def test_cc2_counts_printer_topics_but_not_our_own_echoes():
    p = CC2Connection("a", "10.0.0.1", "A")
    p._mqtt_serial = "SN1"
    await p._handle_mqtt_message(_Msg("elegoo/SN1/c1/api_request", {"method": 1002}))
    await p._handle_mqtt_message(_Msg("elegoo/SN1/api_register", {"client_id": "c1"}))
    assert p.last_seen is None
    await p._handle_mqtt_message(_Msg("elegoo/SN1/api_status", {"result": {"machine_status": {"status": 1}}}))
    assert p.last_seen is not None


@pytest.mark.asyncio
async def test_moonraker_and_prusa_mark_seen_on_a_successful_poll(monkeypatch):
    from printers.moonraker import MoonrakerConnection
    from printers.prusa import PrusaConnection
    import printers.moonraker as mr
    import printers.prusa as pr

    async def stop(*a):
        raise asyncio.CancelledError

    for cls, mod in ((MoonrakerConnection, mr), (PrusaConnection, pr)):
        p = cls("a", "10.0.0.1", "A")

        async def req(*a, **k):
            return {"result": {"status": {}}}
        p._req = req
        monkeypatch.setattr(mod.asyncio, "sleep", stop)
        monkeypatch.setattr(p, "_apply_status", lambda *a, **k: None)
        async def noop(*a, **k):
            pass
        monkeypatch.setattr(p, "_check_print_transition", noop)
        with pytest.raises(asyncio.CancelledError):
            await p.connect()
        assert p.last_seen is not None, cls.__name__


# ── history migration ───────────────────────────────────────────────────────

def test_migration_reads_and_writes_under_one_lock(monkeypatch):
    persistence.HISTORY_FILE.write_text(json.dumps([{"filename": "a"}, {"id": "x" * 32}]))
    held = []
    real = persistence.load_history

    def spying_load():
        held.append(persistence._HISTORY_LOCK.locked())
        return real()
    monkeypatch.setattr(persistence, "load_history", spying_load)
    persistence.migrate_history_ids()
    assert held == [True]
    h = real()
    assert len(h[0]["id"]) == 32 and h[1]["id"] == "x" * 32


def test_migration_is_a_noop_when_every_entry_has_an_id():
    persistence.append_history({"id": "a" * 32})
    before = persistence.HISTORY_FILE.read_text()
    persistence.migrate_history_ids()
    assert persistence.HISTORY_FILE.read_text() == before


# ── the browser's clock (runs the real frontend code under node) ────────────

APP_JS = Path(__file__).resolve().parent.parent / "public" / "app.js"


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    src = APP_JS.read_text()
    block = re.search(r"// clock-sync:begin(.*?)// clock-sync:end", src, re.S).group(1)
    prelude = "function isActivelyPrinting(p){return p.printing===true}\n"
    out = subprocess.run([node, "-e", prelude + block + script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_browser_clock_ahead_of_the_server_does_not_make_fresh_data_look_stale():
    out = _run_node("""
      const realNow = Date.now;
      const T = 1760000000;                       // the server's "now"
      Date.now = () => (T + 3600) * 1000;         // browser clock is an hour AHEAD
      _noteServerTime(T);
      const p = {connected: true, last_seen: T - 5, printing: true};
      console.log(JSON.stringify([isStale(p), formatAgo(p.last_seen)]));
    """)
    assert out == '[false,"5s ago"]'


def test_browser_clock_behind_the_server_still_flags_old_data():
    out = _run_node("""
      const T = 1760000000;
      Date.now = () => (T - 7200) * 1000;         // browser clock two hours BEHIND
      _noteServerTime(T);
      const p = {connected: true, last_seen: T - 200, printing: false};
      console.log(JSON.stringify([isStale(p), formatAgo(p.last_seen)]));
    """)
    assert out == '[true,"3m ago"]'


def test_without_a_server_time_the_browser_clock_is_used_as_before():
    out = _run_node("""
      Date.now = () => 1760000100 * 1000;
      _noteServerTime(undefined); _noteServerTime("nope");
      console.log(JSON.stringify([isStale({connected: true, last_seen: 1760000000, printing: true}), formatAgo(1760000090)]));
    """)
    assert out == '[true,"10s ago"]'
