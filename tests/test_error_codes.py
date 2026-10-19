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
