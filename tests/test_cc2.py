"""printers/cc2.py — error_code capture and reason surfacing."""

import json

import pytest

from printers.cc2 import CC2Connection


class _FakePayload:
    def __init__(self, data: dict):
        self._raw = json.dumps(data).encode()

    def decode(self):
        return self._raw.decode()


class _FakeMessage:
    def __init__(self, topic: str, data: dict):
        self.topic = topic
        self.payload = _FakePayload(data)


@pytest.fixture
def printer():
    p = CC2Connection("pid1", "10.0.0.5", "Test CC2")
    p.connected = True
    p._mqtt_serial = "SN123"
    p._mqtt_client_id = "cli1"
    p._mqtt_registered = True
    return p


def test_protocol_reason_hint_none_without_error_code(printer):
    assert printer._protocol_reason_hint() is None


def test_protocol_reason_hint_none_when_error_code_is_zero(printer):
    printer._cc2_state["error_code"] = 0
    assert printer._protocol_reason_hint() is None


def test_protocol_reason_hint_surfaces_raw_code_only(printer):
    printer._cc2_state["error_code"] = 42
    printer._cc2_state["machine_status"] = {"sub_status": 2501}
    hint = printer._protocol_reason_hint()
    assert hint["code"] == 42
    assert hint["category"] == "unknown"  # never guessed -- not in error_codes.py
    assert hint["message"] == ""
    assert hint["raw"] == {"error_code": 42, "sub_status": 2501}


def test_protocol_reason_hint_resolves_known_error_code(printer):
    printer._cc2_state["error_code"] = 704
    hint = printer._protocol_reason_hint()
    assert hint["code"] == 704
    assert hint["category"] == "leveling"
    assert hint["message"] == "Leveling failed. Please try again."


def test_apply_cc2_status_maps_bed_preheating_sub_status_1906(printer):
    # Verified against Elegoo's own elegoo-link SDK -- 1906 was missing from
    # our sub_status table even though 1405/1096 (also preheating) were there.
    printer._cc2_state["machine_status"] = {"sub_status": 1906}
    printer._apply_cc2_status()
    assert printer.status["PrintInfo"]["Status"] == 15


@pytest.mark.parametrize("sub_status,expected_code", [
    (2801, 1),   # homing
    (2802, 1),   # homing
    (2901, 20),  # auto-leveling
    (2902, 20),  # auto-leveling
])
def test_apply_cc2_status_maps_homing_and_leveling_sub_statuses(printer, sub_status, expected_code):
    printer._cc2_state["machine_status"] = {"sub_status": sub_status}
    printer._apply_cc2_status()
    assert printer.status["PrintInfo"]["Status"] == expected_code


@pytest.mark.asyncio
async def test_error_code_captured_from_api_response_poll_path(printer):
    # Simulates the 5s status poller's method 1003 response, which goes
    # through _CC2_STATE_KEYS filtering that would otherwise drop a scalar
    # like error_code entirely.
    msg = _FakeMessage(
        "elegoo/SN123/cli1/api_response",
        {"method": 1003, "result": {"machine_status": {"sub_status": 0}, "error_code": 7}},
    )
    await printer._handle_mqtt_message(msg)
    assert printer._cc2_state.get("error_code") == 7


@pytest.mark.asyncio
async def test_error_code_captured_from_api_status_push_path(printer):
    msg = _FakeMessage(
        "elegoo/SN123/api_status",
        {"result": {"print_status": {"state": "printing"}, "error_code": 13}},
    )
    await printer._handle_mqtt_message(msg)
    assert printer._cc2_state.get("error_code") == 13


# ── external_device.camera ───────────────────────────────────────────────────

def test_camera_connected_defaults_to_unknown(printer):
    assert printer.camera_connected is None


def test_camera_connected_true_when_reported(printer):
    printer._cc2_state["external_device"] = {"camera": True}
    printer._apply_cc2_status()
    assert printer.camera_connected is True


def test_camera_connected_false_when_reported(printer):
    printer._cc2_state["external_device"] = {"camera": False}
    printer._apply_cc2_status()
    assert printer.camera_connected is False


def test_camera_connected_stays_unknown_without_external_device(printer):
    printer._apply_cc2_status()
    assert printer.camera_connected is None


# ── Device attributes (method 1001) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_attributes_captured_from_api_response(printer):
    msg = _FakeMessage(
        "elegoo/SN123/cli1/api_response",
        {"method": 1001, "result": {
            "machine_model": "Centauri Carbon 2",
            "software_version": {"mcu_version": "00.00.00.00", "ota_version": "02.01.00.00", "soc_version": ""},
            "sn": "F013B3B8WZZ9K11",
            "hostname": "CC_2",
        }},
    )
    await printer._handle_mqtt_message(msg)
    assert printer.attrs == {
        "Model":           "Centauri Carbon 2",
        "FirmwareVersion": "02.01.00.00",
        "MainboardID":     "F013B3B8WZZ9K11",
        "Hostname":        "CC_2",
    }


def test_attrs_untouched_without_device_attribute_fields(printer):
    printer._cc2_state["machine_status"] = {"sub_status": 0}
    printer._apply_cc2_status()
    assert printer.attrs == {}


# ── T2: exception_status, command-result codes, machine_status.status ────────
# Fixtures follow the shapes Elegoo's elegoo-link SDK reads (see
# spooler-cc2-research.md); they are NOT captured from the user's printer.

def test_reason_hint_reads_exception_status_list(printer):
    printer._cc2_state["machine_status"] = {"status": 2, "sub_status": 2502, "exception_status": [1260]}
    printer._cc2_state["exception"] = {"exception_code": {"1260": {"time": 1760000000}}}
    hint = printer._protocol_reason_hint()
    assert hint["code"] == 1260
    assert hint["category"] == "filament_runout"
    assert hint["raw"]["exception_status"] == [1260]
    assert hint["raw"]["exception_times"] == {"1260": 1760000000}


def test_reason_hint_prefers_known_code_and_keeps_action(printer):
    printer._cc2_state["machine_status"] = {"exception_status": [9999999, 1241]}
    hint = printer._protocol_reason_hint()
    assert hint["code"] == 1241
    assert "PTFE" in hint["action"]


def test_reason_hint_unknown_exception_code_stays_raw(printer):
    printer._cc2_state["machine_status"] = {"exception_status": [4242]}
    hint = printer._protocol_reason_hint()
    assert hint["code"] == 4242 and hint["category"] == "unknown" and hint["message"] == ""


def test_stale_exception_dict_without_active_code_gives_no_hint(printer):
    # exception_code dict deep-merges and keeps old keys; only exception_status is current
    printer._cc2_state["machine_status"] = {"exception_status": []}
    printer._cc2_state["exception"] = {"exception_code": {"1260": {"time": 1}}}
    assert printer._protocol_reason_hint() is None


def test_command_result_error_code_is_not_a_printer_fault(printer):
    printer._cc2_state["error_code"] = 1009  # "printer busy", a reply to a command
    assert printer._protocol_reason_hint() is None


@pytest.mark.asyncio
async def test_exception_block_is_stored_from_status_push(printer):
    msg = _FakeMessage(
        "elegoo/SN123/api_status",
        {"result": {"exception": {"exception_code": {"803": {"time": 5}}}}},
    )
    await printer._handle_mqtt_message(msg)
    assert printer._cc2_state["exception"]["exception_code"]["803"]["time"] == 5


@pytest.mark.parametrize("ms_status,expected", [
    (14, 14),  # emergency stop -> error
    (15, 12),  # power-loss recovery -> recovering
    (3, 10), (4, 10),  # filament load/unload -> preparing
    (5, 20),   # auto-leveling outside a print
    (10, 10),  # homing
    (1, 0),    # idle stays idle
])
def test_machine_status_maps_to_display_codes(printer, ms_status, expected):
    printer._cc2_state["machine_status"] = {"status": ms_status, "sub_status": 0}
    printer._apply_cc2_status()
    assert printer.status["PrintInfo"]["Status"] == expected


def test_machine_status_does_not_override_active_print(printer):
    printer._cc2_state["machine_status"] = {"status": 2, "sub_status": 2075}
    printer._cc2_state["print_status"] = {"state": "printing", "print_duration": 10, "remaining_time_sec": 100}
    printer._apply_cc2_status()
    assert printer.status["PrintInfo"]["Status"] == 3


@pytest.mark.parametrize("ms,sub,expected_phase", [
    (2, 1405, "Heating bed"),
    (2, 1045, "Heating nozzle"),
    (2, 2801, "Homing"),
    (2, 2901, "Leveling"),
    (5, 2901, "Leveling"),
    (3, 1133, "Loading filament"),
    (4, 1144, "Unloading filament"),
    (10, 2801, "Homing"),
    (8, 0, "Self-check"),
    (14, 0, "Emergency stop"),
    (15, 0, "Recovering after power loss"),
    (1, 0, ""),
])
def test_phase_label_for_busy_states(printer, ms, sub, expected_phase):
    printer._cc2_state["machine_status"] = {"status": ms, "sub_status": sub}
    printer._apply_cc2_status()
    assert printer.phase == expected_phase


def test_phase_empty_while_printing_and_in_to_dict(printer):
    printer._cc2_state["machine_status"] = {"status": 2, "sub_status": 2075}
    printer._cc2_state["print_status"] = {"state": "printing", "print_duration": 10, "remaining_time_sec": 100}
    printer._apply_cc2_status()
    assert printer.phase == ""
    assert printer.to_dict()["phase"] == ""


# ── heating before a print: state says "printing" but nothing is extruding yet ─

@pytest.mark.parametrize("sub,expected_code,expected_phase", [
    (1045, 15, "Heating nozzle"),   # the real capture: state=printing, duration 0, sub 1045
    (1405, 15, "Heating bed"),
    (2801, 1, "Homing"),
    (2901, 20, "Leveling"),
])
def test_pre_print_phases_win_over_printing_state_while_duration_is_zero(printer, sub, expected_code, expected_phase):
    printer._cc2_state["machine_status"] = {"status": 2, "sub_status": sub}
    printer._cc2_state["print_status"] = {"state": "printing", "print_duration": 0, "remaining_time_sec": 2989}
    printer._apply_cc2_status()
    assert printer.status["PrintInfo"]["Status"] == expected_code
    assert printer.phase == expected_phase
    assert printer.to_dict()["state"] == "preparing"


def test_actual_printing_stays_printing_once_extruding(printer):
    printer._cc2_state["machine_status"] = {"status": 2, "sub_status": 2075}
    printer._cc2_state["print_status"] = {"state": "printing", "print_duration": 120, "remaining_time_sec": 2800}
    printer._apply_cc2_status()
    assert printer.status["PrintInfo"]["Status"] == 3 and printer.to_dict()["state"] == "printing"


def test_midprint_reheat_keeps_printing_state_but_shows_phase(printer):
    printer._cc2_state["machine_status"] = {"status": 2, "sub_status": 1045}
    printer._cc2_state["print_status"] = {"state": "printing", "print_duration": 900, "remaining_time_sec": 2000}
    printer._apply_cc2_status()
    assert printer.to_dict()["state"] == "printing" and printer.phase == "Heating nozzle"


def test_unknown_sub_status_before_extrusion_is_preparing_not_printing(printer, capsys):
    # sub_status 1066 was observed with print_duration 0 during bed leveling.
    printer._cc2_state["machine_status"] = {"status": 2, "sub_status": 1066}
    printer._cc2_state["print_status"] = {"state": "printing", "print_duration": 0, "remaining_time_sec": 2989}
    printer._apply_cc2_status()
    assert printer.to_dict()["state"] == "preparing"
    printer._apply_cc2_status()   # logged only once
    assert capsys.readouterr().out.count("1066") == 1


def test_known_extruding_sub_status_with_zero_duration_still_printing(printer):
    printer._cc2_state["machine_status"] = {"status": 2, "sub_status": 2075}
    printer._cc2_state["print_status"] = {"state": "printing", "print_duration": 0, "remaining_time_sec": 100}
    printer._apply_cc2_status()
    assert printer.to_dict()["state"] == "printing"
