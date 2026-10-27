"""validate_command: every reason an advanced command is refused."""

import pytest

import control
import printer_actions as pa
from printers.cc2 import CC2Connection


@pytest.fixture
def cc2():
    p = CC2Connection("a", "10.0.0.1", "A")
    p.connected = True
    return p


def v(p, action, **params):
    return control.validate_command(p, action, params)


def test_refused_when_the_feature_is_off(cc2):
    assert control.validate_command(cc2, "heaters_off", {}, enabled=False) == "Advanced control is turned off"


def test_refused_when_offline(cc2):
    cc2.connected = False
    assert v(cc2, "heaters_off") == "Printer not connected"


@pytest.mark.parametrize("heater,target,ok", [
    ("nozzle", 210, True), ("nozzle", 0, True), ("nozzle", 301, False), ("nozzle", -1, False),
    ("bed", 100, True), ("bed", 101, False), ("chamber", 40, False), ("nozzle", "hot", False),
    ("nozzle", True, False), ("nope", 10, False),
])
def test_temperature_limits(cc2, heater, target, ok):
    assert (v(cc2, "set_temperature", heater=heater, target=target) is None) is ok


@pytest.mark.parametrize("fan,pct,ok", [("part", 0, True), ("part", 100, True), ("part", 101, False),
                                        ("part", -5, False), ("bogus", 50, False)])
def test_fan_limits(cc2, fan, pct, ok):
    assert (v(cc2, "set_fan", fan=fan, percent=pct) is None) is ok


def test_axes_refused_while_printing(cc2):
    cc2.status = {"PrintInfo": {"Status": 13}}
    assert control.display_state(cc2) == "printing"
    assert "printing" in v(cc2, "move_axis", axis="X", distance=1)
    assert "printing" in v(cc2, "home", axes="XYZ")
    assert v(cc2, "heaters_off") is None   # heat-off always allowed


@pytest.mark.parametrize("axis,dist,ok", [("X", 1, True), ("Z", -10, True), ("Z", 11, False),
                                          ("W", 1, False), ("X", 0, False), ("X", "1", False)])
def test_move_validation_when_idle(cc2, axis, dist, ok):
    assert (v(cc2, "move_axis", axis=axis, distance=dist) is None) is ok


def test_home_rejects_unknown_axes(cc2):
    assert v(cc2, "home", axes="XYZ") is None
    assert v(cc2, "home", axes="XQ") is not None


def test_printer_without_capabilities_refuses_everything():
    from printers.base import PrinterConnection
    p = PrinterConnection("a", "10.0.0.1", "A")
    p.connected = True
    assert v(p, "set_temperature", heater="nozzle", target=10) is not None
    assert v(p, "move_axis", axis="X", distance=1) is not None


async def test_unverified_protocol_reports_not_supported(cc2, monkeypatch, tmp_path):
    import features
    monkeypatch.setattr(control, "CONTROL_LOG", tmp_path / "log.json")
    monkeypatch.setattr(features, "is_enabled", lambda k: True)
    assert await pa.set_temperature(cc2, "nozzle", 200) == "Not supported by this printer yet"
    assert "Not supported" in (tmp_path / "log.json").read_text()
