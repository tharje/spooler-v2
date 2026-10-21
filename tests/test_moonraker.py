"""printers/moonraker.py — message categorization and reason surfacing."""

import pytest

from printers.moonraker import MoonrakerConnection, categorize_moonraker_message


@pytest.mark.parametrize("message,expected", [
    ("", "unknown"),
    (None, "unknown"),
    ("Filament sensor triggered: runout detected", "filament_runout"),
    ("Extruder filament sensor", "filament_runout"),
    ("Heater bed not heating at expected rate", "thermal"),
    ("MCU 'mcu' shutdown: Timer too close", "unknown"),
    ("Some completely unrelated message", "unknown"),
])
def test_categorize_moonraker_message(message, expected):
    assert categorize_moonraker_message(message) == expected


@pytest.fixture
def printer():
    return MoonrakerConnection("pid1", "10.0.0.5", "Test Moonraker")


def test_protocol_reason_hint_none_when_no_message(printer):
    assert printer._protocol_reason_hint() is None


def test_apply_status_reads_layer_count_from_print_stats_info(printer):
    printer._apply_status({
        "print_stats": {"state": "printing", "info": {"current_layer": 12, "total_layer": 80}},
        "display_status": {"progress": 0.15},
        "extruder": {}, "heater_bed": {}, "webhooks": {},
    })
    pi = printer.status["PrintInfo"]
    assert pi["CurrentLayer"] == 12 and pi["TotalLayer"] == 80


def test_apply_status_layer_count_zero_when_slicer_never_set_it(printer):
    printer._apply_status({
        "print_stats": {"state": "printing"},
        "display_status": {"progress": 0.15},
        "extruder": {}, "heater_bed": {}, "webhooks": {},
    })
    pi = printer.status["PrintInfo"]
    assert pi["CurrentLayer"] == 0 and pi["TotalLayer"] == 0


def test_protocol_reason_hint_surfaces_print_stats_message(printer):
    printer._apply_status({
        "print_stats": {"state": "paused", "message": "Filament runout detected"},
    })
    hint = printer._protocol_reason_hint()
    assert hint["message"] == "Filament runout detected"
    assert hint["category"] == "filament_runout"
    assert hint["raw"]["print_stats_message"] == "Filament runout detected"


def test_protocol_reason_hint_falls_back_to_webhooks_state_message(printer):
    printer._apply_status({
        "print_stats": {"state": "error"},
        "webhooks": {"state_message": "Shutdown due to heater fault"},
    })
    hint = printer._protocol_reason_hint()
    assert hint["message"] == "Shutdown due to heater fault"
    assert hint["category"] == "thermal"


@pytest.mark.asyncio
async def test_pause_with_moonraker_message_attributed_to_printer(printer):
    printer.connected = True
    printer._apply_status({"print_stats": {"state": "printing"}})
    await printer._check_print_transition()

    printer._apply_status({
        "print_stats": {"state": "paused", "message": "Filament runout detected"},
    })
    await printer._check_print_transition()

    assert printer.state_reason["kind"] == "pause"
    assert printer.state_reason["initiated_by"] == "printer"
    assert printer.state_reason["category"] == "filament_runout"
    assert printer.state_reason["message"] == "Filament runout detected"
