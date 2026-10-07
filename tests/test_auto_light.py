"""Per-printer option: light on when a print starts, off when it ends."""

import asyncio

import pytest

import persistence
import printers.base as base
from printers.base import PrinterConnection
from printers.cc1 import CC1Connection
from printers.cc2 import CC2Connection


@pytest.fixture
def printer(monkeypatch):
    p = PrinterConnection("pid1", "10.0.0.5", "Bench")
    p.connected = True
    p.supports_light = True
    p.auto_light = True
    calls = []

    async def set_light(on):
        calls.append(on)
        return True
    p.set_light = set_light
    p.calls = calls
    return p


@pytest.mark.asyncio
async def test_switches_on_then_off(printer):
    await printer._auto_light(True)
    await printer._auto_light(False)
    assert printer.calls == [True, False]


@pytest.mark.asyncio
async def test_option_off_or_unsupported_or_offline_does_nothing(printer):
    printer.auto_light = False
    await printer._auto_light(True)
    printer.auto_light, printer.supports_light = True, False
    await printer._auto_light(True)
    printer.supports_light, printer.connected = True, False
    await printer._auto_light(True)
    assert printer.calls == []


@pytest.mark.asyncio
async def test_no_command_when_light_is_already_in_the_wanted_state(printer):
    printer.status = {"LightStatus": {"SecondLight": 1}}
    await printer._auto_light(True)
    assert printer.calls == []
    await printer._auto_light(False)
    assert printer.calls == [False]


@pytest.mark.asyncio
async def test_a_failing_light_never_raises(printer):
    async def boom(on):
        raise RuntimeError("x")
    printer.set_light = boom
    await printer._auto_light(True)


@pytest.mark.asyncio
async def test_end_picture_is_taken_before_the_light_goes_off(printer, monkeypatch):
    order = []

    async def picture(entry_id):
        order.append("picture")
    printer._save_print_picture = picture
    printer.set_light = lambda on: _record(order, on)
    await printer._finish_print_extras("a" * 32)
    assert order == ["picture", False]


async def _record(order, on):
    order.append(on)
    return True


@pytest.mark.asyncio
async def test_light_goes_off_even_when_the_picture_step_fails(printer):
    async def picture(entry_id):
        raise RuntimeError("camera")
    printer._save_print_picture = picture
    with pytest.raises(RuntimeError):
        await printer._finish_print_extras("a" * 32)
    assert printer.calls == [False]


@pytest.mark.asyncio
async def test_print_start_and_end_drive_the_light(printer, monkeypatch):
    monkeypatch.setattr(base, "get_spool_density", lambda pid: 1.24)
    monkeypatch.setattr(base, "spoolman_deduct", lambda *a, **k: None)
    monkeypatch.setattr(base, "spoolman_deduct_spool", lambda *a, **k: None)

    async def noop(msg):
        pass
    monkeypatch.setattr(base.state, "broadcast_to_browsers", noop)
    printer._save_print_picture = lambda eid: asyncio.sleep(0)

    printer.status = {"PrintInfo": {"Status": 0}}
    await printer._check_print_transition()
    printer.status = {"PrintInfo": {"Status": 13, "Filename": "a.gcode"}}
    await printer._check_print_transition()
    await asyncio.sleep(0.05)
    assert printer.calls == [True]
    printer.status = {"PrintInfo": {"Status": 9, "Filename": "a.gcode", "TotalExtrusion": 100}}
    await printer._check_print_transition()
    await asyncio.sleep(0.05)
    assert printer.calls == [True, False]


def test_only_the_centauri_carbons_support_it():
    assert CC1Connection.supports_light and CC2Connection.supports_light
    assert not PrinterConnection.supports_light
    from printers.moonraker import MoonrakerConnection
    from printers.prusa import PrusaConnection
    assert not MoonrakerConnection.supports_light and not PrusaConnection.supports_light


def test_exposed_and_persisted():
    p = CC1Connection("pid1", "10.0.0.5", "Bench")
    assert p.to_dict()["supports_light"] is True and p.to_dict()["auto_light"] is False
    p.auto_light = True
    assert p.to_dict()["auto_light"] is True
    persistence.save_printers({"pid1": p})
    assert persistence.load_printers()[0]["auto_light"] is True
    from printers.prusa import PrusaConnection
    q = PrusaConnection("pid2", "10.0.0.6", "Prusa")
    q.auto_light = True          # can't be honoured, so it isn't reported as on
    assert q.to_dict()["auto_light"] is False


@pytest.mark.asyncio
async def test_cc1_and_cc2_light_commands(monkeypatch):
    sent = []
    p1 = CC1Connection("a", "10.0.0.5", "A")
    async def send1(cmd, data): sent.append(("cc1", cmd, data)); return True
    p1.send_cmd = send1
    await p1.set_light(True)
    p2 = CC2Connection("b", "10.0.0.6", "B")
    async def send2(cmd, data=None): sent.append(("cc2", cmd, data)); return True
    p2.send_cmd = send2
    await p2.set_light(False)
    assert sent[0][2]["LightStatus"]["SecondLight"] is True
    assert sent[1][2] == {"LightStatus": {"SecondLight": False}}
