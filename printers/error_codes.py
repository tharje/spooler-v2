"""
Known CC2 error code meanings.

Every entry here comes from an authoritative source named below -- never guessed
from the numeric value alone. A code missing from this table is surfaced raw
with category "unknown" and no message, per printers/cc2.py's
_protocol_reason_hint(); that is the correct, honest behavior for anything
not yet verified, not a gap to "fill in" with a plausible-sounding guess.

Sources:
  [screen]  verified against a real CC2's touchscreen.
  [fw]      Elegoo's own open-source firmware, ErrorCode namespace in
            github.com/elegooofficial/CentauriCarbon2
            (elegoo/common/exception_handler.h, snapshot from ~Sep 2025).
            The meanings are translations of that file's Chinese code comments
            plus the constant's name; fixes are only given where the comment
            itself names one.
  [sdk]     Elegoo's elegoo-link SDK (the library ElegooSlicer uses), which
            maps 109 to "filament runout".

Fields: category (frontend groups/labels by this), message (short, factual),
action (optional recommended handling, only when a source names one).
"""

CC2_ERROR_CODES = {
    # ── Heaters and temperature sensors ──────────────────────────────────────
    101: {"category": "thermal", "message": "Heated bed isn't heating as expected (heater or thermistor fault)."},      # [fw]
    102: {"category": "thermal", "message": "Heated bed temperature sensor fault."},                                   # [fw]
    103: {"category": "thermal", "message": "Hotend isn't heating as expected (heater fault)."},                       # [fw]
    104: {"category": "thermal", "message": "Hotend temperature sensor fault."},                                       # [fw]
    108: {"category": "thermal", "message": "Heated bed over-temperature."},                                           # [fw]
    902: {"category": "thermal", "message": "Chamber over-temperature."},                                              # [fw]
    # ── Motion and sensors ───────────────────────────────────────────────────
    304: {"category": "motion",  "message": "Z-axis homing failed."},                                                  # [fw]
    401: {"category": "sensor",  "message": "Accelerometer (input-shaper sensor) fault."},                             # [fw]
    # ── Fans ─────────────────────────────────────────────────────────────────
    701: {"category": "fan",     "message": "Mainboard fan fault."},                                                   # [fw]
    702: {"category": "fan",     "message": "Heatbreak (throat) fan fault."},                                          # [fw]
    703: {"category": "fan",     "message": "Model cooling fan fault."},                                               # [fw]
    705: {"category": "fan",     "message": "Auxiliary fan fault."},                                                   # [fw]
    706: {"category": "fan",     "message": "Chamber fan fault."},                                                     # [fw]
    # ── Leveling ─────────────────────────────────────────────────────────────
    # Verified 2026-10-02 against a real CC2's touchscreen (Tharje); the
    # constant is BED_MESH_FAIL in [fw].
    704: {"category": "leveling", "message": "Leveling failed. Please try again."},                                    # [screen] [fw]
    # ── Hardware connections / covers ────────────────────────────────────────
    707: {"category": "hardware", "message": "Toolhead front cover (fan cover) has come off."},                        # [fw]
    801: {"category": "connection", "message": "Lost communication with the extruder/toolhead board."},               # [fw]
    802: {"category": "connection", "message": "Lost communication with the bed/leveling sensor board."},             # [fw]
    803: {"category": "system",  "message": "System error."},                                                         # [fw]
    # ── Filament runout ──────────────────────────────────────────────────────
    109:  {"category": "filament_runout", "message": "Filament ran out."},                                             # [sdk]
    1211: {"category": "filament_runout", "message": "Canvas: filament used up."},                                    # [fw]
    1260: {"category": "filament_runout", "message": "Filament break / runout detected during print."},               # [fw]
    # ── Canvas and filament feed ─────────────────────────────────────────────
    1101: {"category": "hardware", "message": "Canvas: exhaust vent grille failed to open."},                         # [fw]
    1103: {"category": "hardware", "message": "Canvas: exhaust vent grille failed to close."},                        # [fw]
    1210: {"category": "connection", "message": "Canvas: communication lost."},                                       # [fw]
    1220: {"category": "filament_feed", "message": "Abnormal filament detected in the extruder.",
           "action": "Remove the filament from the extruder by hand."},                                              # [fw]
    1231: {"category": "hardware", "message": "Canvas: cutter wasn't pressed in when cutting the filament."},         # [fw]
    1232: {"category": "hardware", "message": "Canvas: cutter didn't release normally after cutting."},               # [fw]
    1241: {"category": "filament_feed", "message": "Feed self-check: filament sensor not triggered.",
           "action": "Check that the PTFE tube of the Canvas channel is plugged in."},                               # [fw]
    1242: {"category": "filament_feed", "message": "Feed self-check timed out.",
           "action": "Filament may be slipping in the channel, or the extruder is blocked."},                        # [fw]
    1243: {"category": "filament_feed", "message": "Feed self-check failed.",
           "action": "The extruder mechanism is probably blocked."},                                                 # [fw]
    1251: {"category": "filament_feed", "message": "Unload self-check failed.",
           "action": "Filament may have broken off inside the extruder."},                                           # [fw]
    1252: {"category": "filament_feed", "message": "Unload self-check timed out.",
           "action": "Filament may be slipping in the channel."},                                                   # [fw]
    1262: {"category": "hardware", "message": "Canvas: cutter fault."},                                               # [fw]
    1263: {"category": "filament_tangle", "message": "Filament tangled (wrap) detected."},                            # [fw]
    1264: {"category": "filament_feed", "message": "External filament holder fault (plug/blockage)."},                # [fw]
}

# Codes the original Centauri Carbon (CC1) shows on its own screen. Source:
# Elegoo's open-source CC1 firmware (github.com/elegooofficial/CentauriCarbon,
# firmware/app/e100/app_top.cpp for the code, firmware/resources/e100/
# translation.csv for the English text -- quoted/condensed, not invented).
# That repository is GPL-3.0; the messages below are taken from it under that
# licence (compatible with this project's AGPL-3.0).
# NOT yet seen arriving over SDCP: CC1's WebSocket status isn't known to carry
# these codes, so nothing looks them up until a real capture shows where they
# come from (see printers/cc1.py _protocol_reason_hint). 101-104, 304 and
# 701-703 mean the same on CC2.
CC1_ERROR_CODES = {
    101: {"category": "thermal", "message": "The heated bed didn't heat up as expected."},
    102: {"category": "thermal", "message": "Anomaly in reading the heated bed NTC.",
          "action": "Check the heated bed NTC and its wiring."},
    103: {"category": "thermal", "message": "The printhead didn't heat up as expected."},
    104: {"category": "thermal", "message": "Anomaly in reading the printhead NTC.",
          "action": "Check the printhead NTC and its wiring."},
    304: {"category": "motion",  "message": "Z-axis returning to home failed (abnormal motor).",
          "action": "Check the Z-axis motor and its wiring."},
    502: {"category": "leveling", "message": "Abnormal leveling sensor.",
          "action": "Check the leveling sensor and its wiring."},
    701: {"category": "fan",     "message": "Abnormal mainboard fan.",
          "action": "Check the mainboard fan and its wiring."},
    702: {"category": "fan",     "message": "Abnormal heat break cooling fan.",
          "action": "Check the heat break cooling fan and its wiring."},
    703: {"category": "fan",     "message": "Abnormal model fan.",
          "action": "Check the model fan and its wiring."},
}

# Result codes of *commands* (api_response result.error_code), not printer
# faults: UNKNOWN_INTERFACE..DATABASE_FAILED, PRINT_FILE_NOT_FOUND,
# MISSING_BED_LEVELING and the 9xxx upload/file group. They never overlap the
# printer exception codes above, and must not be shown as why a print stopped.
_API_RESULT_RANGES = ((1000, 1013), (1021, 1021), (1026, 1026), (9000, 9999))


def is_api_result_code(code) -> bool:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return False
    return any(lo <= code <= hi for lo, hi in _API_RESULT_RANGES)


def lookup(error_code, printer_type: str = "cc2") -> dict | None:
    table = CC1_ERROR_CODES if printer_type == "cc1" else CC2_ERROR_CODES
    try:
        return table.get(int(error_code))
    except (TypeError, ValueError):
        return None
