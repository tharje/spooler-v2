import asyncio

import pytest

from printers.cc1 import CC1Connection, _fan_percents


@pytest.fixture
def cc1(monkeypatch):
    p = CC1Connection("p1", "192.0.2.10", "CC1")
    sent = []

    async def fake_send(cmd, data):
        sent.append((cmd, data))
        return True
    monkeypatch.setattr(p, "send_cmd", fake_send)
    return p, sent


def run(coro):
    return asyncio.run(coro)


def test_temperatures_use_403(cc1):
    p, sent = cc1
    assert run(p.set_temperature("nozzle", 214.6))
    assert run(p.set_temperature("bed", 60))
    assert sent == [(403, {"TempTargetNozzle": 215}), (403, {"TempTargetHotbed": 60})]
    assert run(p.set_temperature("chamber", 40)) is False


def test_heaters_off_in_one_command(cc1):
    p, sent = cc1
    assert run(p.heaters_off(["nozzle", "bed"]))
    assert sent == [(403, {"TempTargetNozzle": 0, "TempTargetHotbed": 0})]


def test_fans_in_percent(cc1):
    p, sent = cc1
    run(p.set_fan("part", 55))
    run(p.set_fan("aux", 140))
    run(p.set_fan("chamber", 0))
    assert sent == [(403, {"TargetFanSpeed": {"ModelFan": 55}}),
                    (403, {"TargetFanSpeed": {"AuxiliaryFan": 100}}),
                    (403, {"TargetFanSpeed": {"BoxFan": 0}})]


def test_move_and_home(cc1):
    p, sent = cc1
    run(p.move_axis("x", -10))
    run(p.home("XYZ"))
    run(p.home("Z"))
    run(p.home("XY"))
    assert sent == [(401, {"Axis": "X", "Step": -10}), (402, {"Axis": "XYZ"}), (402, {"Axis": "Z"}),
                    (402, {"Axis": "X"}), (402, {"Axis": "Y"})]


def test_no_calibration_over_sdcp(cc1):
    p, sent = cc1
    assert not ({"level", "pid", "shaper"} & p.capabilities)
    assert run(p.auto_level()) is False and sent == []


def test_fan_status_is_normalised():
    assert _fan_percents({"ModelFan": 30, "AuxiliaryFan": 0, "BoxFan": 100}) == {"part": 30, "aux": 0, "chamber": 100}
    assert _fan_percents(None) == {}
