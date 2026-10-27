"""The shared action layer: validation and state handling live in
printer_actions, ws_handler only routes."""

import json

import pytest

import printer_actions as pa
import ws_handler
from printers.protocol import CMD_DELETE_FILES, CMD_LIGHT, CMD_PAUSE, CMD_STOP


class FakePrinter:
    id = "p1"

    def __init__(self, printer_type="cc1", connected=True):
        self.printer_type = printer_type
        self.connected = connected
        self.sent = []
        self.marked = 0
        self.started = []
        self.deleted = []

    async def send_cmd(self, cmd, data=None):
        self.sent.append((cmd, data))
        return self.connected

    def mark_spooler_command(self):
        self.marked += 1

    async def start_print_file(self, filename, print_opts=None):
        self.started.append((filename, print_opts))
        return True

    async def delete_files(self, names):
        self.deleted.append(names)
        return True


@pytest.fixture(autouse=True)
def no_light_refresh(monkeypatch):
    monkeypatch.setattr(pa, "_schedule_light_refresh", lambda p: None)


async def test_pause_and_stop_mark_the_command_as_ours():
    p = FakePrinter()
    assert await pa.pause(p) is None
    assert await pa.stop(p) is None
    assert [c for c, _ in p.sent] == [CMD_PAUSE, CMD_STOP]
    assert p.marked == 2


async def test_offline_printer_gives_an_error_and_is_not_marked():
    p = FakePrinter(connected=False)
    assert await pa.pause(p) == pa.ERR_NOT_CONNECTED
    assert p.marked == 0


async def test_light_command_carries_the_state():
    p = FakePrinter()
    await pa.set_light(p, False)
    cmd, data = p.sent[0]
    assert cmd == CMD_LIGHT and data["LightStatus"]["SecondLight"] is False


@pytest.mark.parametrize("speed", [9, 201, "fast", None])
async def test_speed_outside_range_is_ignored(speed):
    p = FakePrinter()
    assert await pa.set_speed(p, speed) is None
    assert p.sent == []


async def test_speed_uses_the_cc2_method_on_cc2():
    p = FakePrinter("cc2")
    await pa.set_speed(p, 120)
    assert p.sent == [(1031, {"speed": 120})]


@pytest.mark.parametrize("name,err", [("", pa.ERR_NO_FILENAME), ("  ", pa.ERR_NO_FILENAME),
                                      ("../x.gcode", pa.ERR_BAD_FILENAME),
                                      ("a\\..\\b", pa.ERR_BAD_FILENAME)])
async def test_start_print_rejects_bad_filenames(name, err):
    p = FakePrinter()
    assert await pa.start_print(p, name) == err
    assert p.started == []


async def test_start_print_drops_non_dict_options():
    p = FakePrinter()
    assert await pa.start_print(p, "a.gcode", "oops") is None
    assert p.started == [("a.gcode", None)]


async def test_delete_files_validates_the_list():
    p = FakePrinter()
    await pa.delete_files(p, ["a"] * 51)
    await pa.delete_files(p, ["a", ""])
    await pa.delete_files(p, "a")
    assert p.sent == [] and p.deleted == []
    await pa.delete_files(p, ["a", "b"])
    assert p.sent == [(CMD_DELETE_FILES, {"FileList": ["a", "b"]})]
    c = FakePrinter("cc2")
    await pa.delete_files(c, ["a"])
    assert c.deleted == [["a"]]


async def test_delete_file_rejects_traversal():
    p = FakePrinter()
    assert await pa.delete_file(p, "../etc") == pa.ERR_BAD_FILENAME
    assert await pa.delete_file(p, "") == pa.ERR_BAD_FILENAME


async def test_ws_handler_routes_and_reports_errors(monkeypatch):
    import state
    p = FakePrinter(connected=False)
    monkeypatch.setitem(state.printers, "p1", p)
    sent = []

    class WS:
        async def send(self, m):
            sent.append(json.loads(m))

    await ws_handler.handle_browser_message(WS(), json.dumps({"action": "pause", "printer_id": "p1"}))
    assert sent == [{"type": "error", "message": pa.ERR_NOT_CONNECTED}]
