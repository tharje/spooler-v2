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


def test_cc2_can_only_home_all_axes(cc2):
    # Method 1026 always runs a full G28
    assert v(cc2, "home", axes="ZYX") is None
    assert v(cc2, "home", axes="XY") == "This printer can only home all axes at once"
    assert v(cc2, "home", axes="Z") is not None


async def test_cc2_home_sends_method_1026(cc2, monkeypatch, tmp_path):
    import features
    monkeypatch.setattr(control, "CONTROL_LOG", tmp_path / "log.json")
    monkeypatch.setattr(features, "is_enabled", lambda k: True)
    sent = []

    async def fake_send(cmd, data=None):
        sent.append((cmd, data))
        return True
    monkeypatch.setattr(cc2, "send_cmd", fake_send)
    cc2._cc2_state["extruder"] = {"target": 200}   # nozzle on: no post-homing watcher
    assert await pa.home(cc2, "XYZ") is None
    assert sent == [(1026, {"homed_axes": "xyz"})]


async def _run_home_cleanup(cc2, monkeypatch, steps, target):
    """Feed sub_status values to the post-homing watcher; return what it sent."""
    sent = []

    async def fake_send(cmd, data=None):
        sent.append((cmd, data))
        return True
    monkeypatch.setattr(cc2, "send_cmd", fake_send)
    it = iter(steps)

    async def fake_sleep(_):
        sub = next(it, steps[-1])
        cc2._cc2_state["machine_status"] = {"sub_status": sub}
        cc2._cc2_state["extruder"] = {"target": target}
    monkeypatch.setattr("printers.cc2.asyncio.sleep", fake_sleep)
    await cc2._nozzle_off_after_homing(timeout_s=60)
    return sent


async def test_nozzle_is_switched_off_after_homing(cc2, monkeypatch):
    # Stale 2802 from an earlier homing first, then this homing runs and the
    # printer goes idle without the closing 2802 being polled
    sent = await _run_home_cleanup(cc2, monkeypatch, [2802, 2801, 1045, 1066, 0], 140)
    assert sent == [(1028, {"extruder": 0})]


async def test_stale_status_before_homing_does_not_end_the_wait(cc2, monkeypatch):
    # Nothing homing-related seen yet: keep waiting (until the timeout)
    steps = [0, 0, 2801, 1045]
    sent = []

    async def fake_send(cmd, data=None):
        sent.append((cmd, data))
        return True
    monkeypatch.setattr(cc2, "send_cmd", fake_send)
    it = iter(steps)

    async def fake_sleep(_):
        sub = next(it, 1045)
        cc2._cc2_state["machine_status"] = {"sub_status": sub}
        cc2._cc2_state["extruder"] = {"target": 140}
        if sub == 1045:
            cc2._cc2_state["extruder"] = {"target": 210}   # user preheats while waiting
    monkeypatch.setattr("printers.cc2.asyncio.sleep", fake_sleep)
    await cc2._nozzle_off_after_homing(timeout_s=0.05)
    assert sent == []


async def test_nozzle_heat_set_meanwhile_is_left_alone(cc2, monkeypatch):
    sent = await _run_home_cleanup(cc2, monkeypatch, [2801, 2802], 210)
    assert sent == []


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
    assert await pa.set_fan(cc2, "part", 50) == "Not supported by this printer yet"
    assert "Not supported" in (tmp_path / "log.json").read_text()


@pytest.mark.parametrize("heater,target,expected", [
    ("nozzle", 210, {"extruder": 210}),
    ("bed", 60.4, {"heater_bed": 60}),
    ("nozzle", 0, {"extruder": 0}),
])
async def test_cc2_set_temperature_sends_method_1028(cc2, monkeypatch, tmp_path, heater, target, expected):
    import features
    monkeypatch.setattr(control, "CONTROL_LOG", tmp_path / "log.json")
    monkeypatch.setattr(features, "is_enabled", lambda k: True)
    sent = []

    async def fake_send(cmd, data=None):
        sent.append((cmd, data))
        return True
    monkeypatch.setattr(cc2, "send_cmd", fake_send)
    assert await pa.set_temperature(cc2, heater, target) is None
    assert sent == [(1028, expected)]


async def test_cc2_temperature_above_limit_is_not_sent(cc2, monkeypatch, tmp_path):
    import features
    monkeypatch.setattr(control, "CONTROL_LOG", tmp_path / "log.json")
    monkeypatch.setattr(features, "is_enabled", lambda k: True)
    sent = []

    async def fake_send(cmd, data=None):
        sent.append((cmd, data))
        return True
    monkeypatch.setattr(cc2, "send_cmd", fake_send)
    assert await pa.set_temperature(cc2, "nozzle", 301) is not None
    assert await pa.set_temperature(cc2, "bed", 101) is not None
    assert sent == []


async def test_cc2_heaters_off_is_one_command_for_both_heaters(cc2, monkeypatch, tmp_path):
    import features
    monkeypatch.setattr(control, "CONTROL_LOG", tmp_path / "log.json")
    monkeypatch.setattr(features, "is_enabled", lambda k: True)
    sent = []

    async def fake_send(cmd, data=None):
        sent.append((cmd, data))
        return True
    monkeypatch.setattr(cc2, "send_cmd", fake_send)
    assert await pa.heaters_off(cc2) is None
    assert sent == [(1028, {"extruder": 0, "heater_bed": 0})]


def test_auto_level_needs_capability_and_idle_printer(cc2):
    assert v(cc2, "auto_level") is None
    cc2.status = {"PrintInfo": {"Status": 13}}
    assert "printing" in v(cc2, "auto_level")
    from printers.base import PrinterConnection
    p = PrinterConnection("a", "10.0.0.1", "A")
    p.connected = True
    assert v(p, "auto_level") is not None
