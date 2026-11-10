"""printers/error_codes.py — verified CC2 error_code catalog."""

from printers.error_codes import lookup


def test_lookup_known_code():
    entry = lookup(704)
    assert entry["category"] == "leveling"
    assert entry["message"] == "Leveling failed. Please try again."


def test_lookup_unknown_code_returns_none():
    assert lookup(999999) is None


def test_every_entry_has_category_and_message():
    from printers.error_codes import CC2_ERROR_CODES
    assert len(CC2_ERROR_CODES) > 30
    for code, entry in CC2_ERROR_CODES.items():
        assert isinstance(code, int)
        assert entry["category"] and entry["message"], code


def test_lookup_accepts_string_codes():
    assert lookup("704")["category"] == "leveling"
    assert lookup("not a number") is None


def test_api_result_codes_are_not_printer_faults():
    from printers.error_codes import CC2_ERROR_CODES, is_api_result_code
    assert is_api_result_code(1009) and is_api_result_code(9004) and is_api_result_code(1026)
    assert not is_api_result_code(704) and not is_api_result_code(1243) and not is_api_result_code(109)
    # a code can't be both a catalogued fault and a command result
    assert not any(is_api_result_code(c) for c in CC2_ERROR_CODES)


def test_cc1_lookup_uses_the_cc1_table_not_cc2():
    # 402 (Y-axis accelerometer) exists for CC1 but not CC2 -- CC2 has a single
    # combined 401. Picking the wrong table would silently return None instead
    # of the real entry. (302/303 used to be the example here, but the .13
    # firmware shows CC2 carries X/Y homing codes too, so they're in both now.)
    assert lookup(402, "cc1")["category"] == "sensor"
    assert lookup(402, "cc2") is None
    # 302 X-axis homing is now known for CC2 as well (from the .13 screen).
    assert lookup(302, "cc2")["category"] == "motion"


def test_cc1_accelerometer_codes_split_by_axis():
    x, y = lookup(401, "cc1"), lookup(402, "cc1")
    assert x["category"] == y["category"] == "sensor"
    assert x["message"] != y["message"]  # same fault, but each names its axis


def test_gui_sourced_codes_are_present():
    # Codes added 2026-11-10 from the .13 touchscreen binary (every code its
    # error table carries). A representative spread across the new groups.
    from printers.error_codes import CC2_ERROR_CODES
    for code in (105, 106, 107, 110, 205, 206, 302, 303, 502, 605, 708, 709,
                 901, 903, 904, 905, 906, 907, 1102, 1202, 1205, 1209, 1266,
                 1267, 1301, 1302, 2002, 2004):
        assert code in CC2_ERROR_CODES, code
        assert CC2_ERROR_CODES[code]["category"], code
        assert CC2_ERROR_CODES[code]["message"], code
    # The catalogue grew well past the old ~30.
    assert len(CC2_ERROR_CODES) >= 70


def test_every_cc1_entry_has_category_and_message():
    from printers.error_codes import CC1_ERROR_CODES
    assert len(CC1_ERROR_CODES) >= 13
    for code, entry in CC1_ERROR_CODES.items():
        assert isinstance(code, int)
        assert entry["category"] and entry["message"], code
