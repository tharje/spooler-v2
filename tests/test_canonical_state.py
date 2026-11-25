"""printers/base.py — canonical numeric state.

The protocol-key fallback chains (CC1 SDCP names vs CC2/Prusa/Moonraker
names) and the progress rules the frontend's getProgress() uses live here,
once, in to_dict(). Every consumer of printer state — browser UI, MQTT
bridge, future channels — reads these keys and owns no protocol logic.
"""

from printers.base import PrinterConnection


CC1_STATUS = {"TempOfNozzle": 210, "TempTargetNozzle": 215, "TempOfHotbed": 60,
              "TempTargetHotbed": 65, "TempOfBox": 30, "SpeedFactor": 95,
              "PrintInfo": {"Status": 3, "CurrentLayer": 12, "TotalLayer": 100,
                            "PrintTime": 120, "RemainTime": 300}}


def _p(status, **attrs):
    p = PrinterConnection("p1", "192.0.2.5", "Workshop")
    p.connected = True
    p.attrs = {"FirmwareVersion": "1.2.3"}
    p.status = status
    return p


def test_canonical_keys_read_cc1_names():
    d = _p(CC1_STATUS).to_dict()
    assert d["nozzle_temp"] == 210 and d["nozzle_target"] == 215
    assert d["bed_temp"] == 60 and d["bed_target"] == 65
    assert d["chamber_temp"] == 30
    assert d["layer"] == 12 and d["total_layer"] == 100
    assert d["time_remaining"] == 300
    assert d["speed_factor"] == 95


def test_canonical_keys_read_cc2_names():
    d = _p({"NozzleTemp": 1, "NozzleTempTarget": 2, "BedTemp": 3,
            "BedTempTarget": 4, "ChamberTemp": 5}).to_dict()
    assert (d["nozzle_temp"], d["nozzle_target"], d["bed_temp"],
            d["bed_target"], d["chamber_temp"]) == (1, 2, 3, 4, 5)


def test_progress_prefers_printer_percentage():
    # TotalTicks == 100 means CurrentTicks IS the percentage (CC2/Moonraker/
    # PrusaLink); it beats layers, which understate progress (issue #56).
    d = _p({"PrintInfo": {"TotalTicks": 100, "CurrentTicks": 42,
                          "TotalLayer": 100, "CurrentLayer": 99}}).to_dict()
    assert d["progress"] == 42


def test_progress_from_layers_when_no_percentage():
    d = _p({"PrintInfo": {"TotalTicks": 700, "CurrentTicks": 40,
                          "TotalLayer": 100, "CurrentLayer": 25}}).to_dict()
    assert d["progress"] == 25


def test_progress_none_when_unknown():
    assert _p({}).to_dict()["progress"] is None


def test_canonical_temps_default_to_zero_like_the_ui():
    d = _p({}).to_dict()
    assert d["nozzle_temp"] == 0 and d["nozzle_target"] == 0
    assert d["bed_temp"] == 0 and d["bed_target"] == 0
    assert d["chamber_temp"] == 0 and d["chamber_target"] == 0
    assert d["layer"] == 0 and d["total_layer"] == 0
    assert d["time_remaining"] == 0 and d["speed_factor"] == 100
