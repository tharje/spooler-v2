import asyncio

import pytest

import config
import features
import idle_guard
import state


class FakePrinter:
    def __init__(self, status=0, nozzle=0, bed=0, connected=True, ok=True):
        self.id, self.name = "p1", "P1"
        self.connected = connected
        self.capabilities = frozenset({"temp_nozzle", "temp_bed"})
        self.status, self.nozzle, self.bed, self.ok = status, nozzle, bed, ok
        self.off_calls = []

    def _decoded_printinfo(self):
        return {"Status": self.status, "TempTargetNozzle": self.nozzle, "TempTargetHotbed": self.bed}

    def _is_busy_between_prints(self):
        return False

    async def heaters_off(self, heaters):
        self.off_calls.append(heaters)
        if self.ok:
            self.nozzle = self.bed = 0
        return self.ok


@pytest.fixture
def guard(monkeypatch):
    values = {"control.idle_heater_min": 30}
    monkeypatch.setattr(config, "get", lambda k: values.get(k))
    monkeypatch.setattr(features, "is_enabled", lambda k: True)
    monkeypatch.setattr(state, "printers", {})
    logged = []
    monkeypatch.setattr(idle_guard.control, "log_command", lambda *a: logged.append(a))
    idle_guard._since.clear()
    yield values, logged
    idle_guard._since.clear()


def run(now):
    return asyncio.run(idle_guard.check_once(now))


def test_switches_off_after_the_set_minutes(guard):
    _, logged = guard
    p = FakePrinter(nozzle=200)
    state.printers["p1"] = p
    assert run(1000) == []
    assert idle_guard.seconds_left("p1", 1000 + 60) == 29 * 60
    assert run(1000 + 29 * 60) == []
    assert run(1000 + 30 * 60) == ["p1"]
    assert p.off_calls == [["nozzle", "bed"]]
    assert logged[0][0] == "Idle guard" and logged[0][3] == {"idle_min": 30}
    assert idle_guard.seconds_left("p1") is None


@pytest.mark.parametrize("status", [2, 6, 20, 1])   # printing, paused, leveling, homing
def test_never_while_busy(guard, status):
    p = FakePrinter(status=status, bed=60)
    state.printers["p1"] = p
    run(0)
    assert run(10 ** 6) == [] and p.off_calls == []
    assert "p1" not in idle_guard._since


def test_timer_restarts_after_a_busy_spell(guard):
    p = FakePrinter(nozzle=200)
    state.printers["p1"] = p
    run(0)
    p.status = 2
    run(20 * 60)
    p.status = 0
    run(25 * 60)
    assert run(50 * 60) == []          # only 25 min idle since the print
    assert run(55 * 60) == ["p1"]


def test_cold_printer_is_left_alone(guard):
    state.printers["p1"] = FakePrinter()
    run(0)
    assert run(10 ** 6) == [] and idle_guard._since == {}


def test_zero_minutes_turns_it_off(guard):
    values, _ = guard
    values["control.idle_heater_min"] = 0
    p = FakePrinter(nozzle=200)
    state.printers["p1"] = p
    run(0)
    assert run(10 ** 6) == [] and p.off_calls == []
    assert idle_guard.seconds_left("p1") is None


def test_off_when_advanced_control_is_off(guard, monkeypatch):
    monkeypatch.setattr(features, "is_enabled", lambda k: False)
    p = FakePrinter(nozzle=200)
    state.printers["p1"] = p
    run(0)
    assert run(10 ** 6) == [] and p.off_calls == []


def test_failure_is_logged_and_not_retried_every_pass(guard):
    _, logged = guard
    p = FakePrinter(nozzle=200, ok=False)
    state.printers["p1"] = p
    run(0)
    assert run(30 * 60) == []
    assert logged[0][4] == "Not supported by this printer yet"
    run(30 * 60 + 20)
    assert len(p.off_calls) == 1
