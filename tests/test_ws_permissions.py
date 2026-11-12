"""Browser WebSocket actions are checked against the user's role, not just
whether they can see the printer."""

import json

import pytest

import auth
import state
import users
import ws_handler
from printers.base import PrinterConnection


class WS:
    def __init__(self, user):
        self._spooler_user = user
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    def denied(self):
        return any(m.get("message") == "You don't have permission to do that." for m in self.sent)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(ws_handler, "AUTH_ENABLED", True)
    users.ensure_initialised()
    users.create_user("boss", "secret1", "admin")
    users.create_user("gran", "secret1", "viewer")
    users.create_user("kid", "secret1", "operator")
    p = PrinterConnection("a", "10.0.0.1", "A")
    p.connected = True                  # no "offline" timer from _broadcast_state
    calls = []

    async def send_cmd(cmd, data=None):
        calls.append(cmd)
        return True
    p.send_cmd = send_cmd
    monkeypatch.setattr(state, "printers", {"a": p})
    monkeypatch.setattr(state, "tray_map", {})

    async def nobody(msg):
        pass
    monkeypatch.setattr(state, "broadcast_to_browsers", nobody)
    monkeypatch.setattr(ws_handler, "save_printers", lambda printers: None)
    monkeypatch.setattr(ws_handler, "save_tray_map", lambda m: None)
    return p, calls


async def _do(user, **msg):
    ws = WS(user)
    await ws_handler.handle_browser_message(ws, json.dumps({"printer_id": "a", **msg}))
    return ws


@pytest.mark.parametrize("action", ["pause", "resume", "stop", "light_on", "set_speed", "set_temperature"])
async def test_viewer_cannot_control_printer(setup, action):
    p, calls = setup
    ws = await _do("gran", action=action, speed=100)
    assert ws.denied() and calls == []


async def test_viewer_cannot_remove_or_rename_printer(setup):
    assert (await _do("gran", action="remove_printer")).denied()
    assert (await _do("gran", action="update_printer", name="Hacked")).denied()
    assert "a" in state.printers and state.printers["a"].name == "A"


async def test_viewer_cannot_start_print_or_link_trays(setup):
    assert (await _do("gran", action="start_print", filename="x.gcode")).denied()
    assert (await _do("gran", action="link_tray", tray_id=0, spool_id=3)).denied()
    assert state.tray_map == {}


async def test_viewer_can_still_look(setup):
    ws = await _do("gran", action="status")
    assert not ws.denied()
    assert not (await _do("gran", action="list_printers")).denied()


async def test_operator_can_control_but_not_manage_printers(setup):
    p, calls = setup
    assert not (await _do("kid", action="pause")).denied()
    assert calls
    assert (await _do("kid", action="remove_printer")).denied()


async def test_admin_can_do_everything(setup):
    assert not (await _do("boss", action="pause")).denied()
    assert not (await _do("boss", action="update_printer", name="B")).denied()
    assert state.printers["a"].name == "B"


async def test_link_tray_needs_to_see_the_printer(setup):
    users.update_role("operator", permissions=["view", "filament.manage"])
    users.set_user_printers("kid", ["other"])
    await _do("kid", action="link_tray", tray_id=0, spool_id=3)
    assert state.tray_map == {}


async def test_unknown_actions_need_printer_control():
    assert ws_handler.ACTION_PERMISSIONS.get("some_future_action") is None
    assert ws_handler.DEFAULT_ACTION_PERMISSION == "printer.control"


async def test_printers_manage_is_separate_from_settings(setup):
    users.create_role("helper", ["view", "settings.manage"])
    users.create_user("helper1", "secret1", "helper")
    assert (await _do("helper1", action="update_printer", name="X")).denied()
    users.update_role("helper", permissions=["view", "printers.manage"])
    assert not (await _do("helper1", action="update_printer", name="X")).denied()
    assert state.printers["a"].name == "X"


def test_roles_with_settings_manage_get_printers_manage_once(tmp_path):
    import persistence
    data = users._load()
    data.pop("printers_manage_split", None)
    data["roles"]["helper"] = {"permissions": ["view", "settings.manage"], "printers": "*"}
    users._save(data)
    users.ensure_initialised()
    assert "printers.manage" in users._load()["roles"]["helper"]["permissions"]
    # Taken away afterwards: stays away.
    users.update_role("helper", permissions=["view", "settings.manage"])
    users.ensure_initialised()
    assert "printers.manage" not in users._load()["roles"]["helper"]["permissions"]


@pytest.mark.parametrize("action", ["set_temperature", "set_fan", "move_axis", "home", "auto_level", "heaters_off", "motors_off"])
async def test_advanced_control_needs_its_own_permission(setup, action):
    assert ws_handler.ACTION_PERMISSIONS[action] == "printer.advanced"
    assert (await _do("kid", action=action)).denied()          # operator: printer.control only
    users.update_role("operator", permissions=["view", "printer.control", "printer.advanced"])
    assert not (await _do("kid", action=action)).denied()
