"""stats.py: every number is checked against a hand count of a tiny history."""

import io

import pytest

import features
import stats

H = [
    # Mon 2026-10-05
    {"id": "1", "timestamp": "2026-10-05T10:00:00", "printer_id": "A", "printer_name": "CC1", "filename": "a.gcode",
     "filament_g": 10.0, "filament_mm": 3000, "print_time_s": 3600, "end_state": "complete", "material": "PLA"},
    {"id": "2", "timestamp": "2026-10-05T20:00:00", "printer_id": "B", "printer_name": "CC2", "filename": "b.gcode",
     "filament_g": 20.5, "filament_mm": 6000, "print_time_s": 7200, "end_state": "complete", "material": "PETG"},
    # Tue 2026-10-06: an error and a user cancel
    {"id": "3", "timestamp": "2026-10-06T09:00:00", "printer_id": "B", "printer_name": "CC2", "filename": "c.gcode",
     "filament_g": 5.0, "filament_mm": 1500, "print_time_s": 1800, "end_state": "error", "error_code": "103",
     "error_message": "Hotend isn't heating", "stop_reason": "thermal", "material": "PETG"},
    {"id": "4", "timestamp": "2026-10-06T12:00:00", "printer_id": "A", "printer_name": "CC1", "filename": "d.gcode",
     "filament_g": 1.5, "filament_mm": 450, "print_time_s": 600, "end_state": "cancelled", "stop_reason": "user"},
    # old-style entries (no end_state / material)
    {"id": "5", "timestamp": "2026-09-01T08:00:00", "printer_id": "A", "printer_name": "CC1", "filename": "old.gcode",
     "filament_g": 8.0, "filament_mm": 2400, "print_time_s": 3000, "completed": True},
    {"id": "6", "timestamp": "2026-09-02T08:00:00", "printer_id": "A", "printer_name": "CC1", "filename": "old2.gcode",
     "filament_g": 2.0, "filament_mm": 600, "print_time_s": 500, "completed": False},
    {"id": "bad", "timestamp": "not a date", "filament_g": 99, "print_time_s": 99999},
]


def test_overall_numbers_match_a_hand_count():
    s = stats.compute_stats(H)
    assert s["prints"] == 6                                  # the unreadable-date entry is skipped
    assert s["results"] == {"complete": 3, "cancelled": 2, "error": 1}
    assert s["grams"] == 47.0                                # 10 + 20.5 + 5 + 1.5 + 8 + 2
    assert s["hours"] == round((3600 + 7200 + 1800 + 600 + 3000 + 500) / 3600, 2)
    assert s["success_rate"] == 50.0
    assert s["avg_print_s"] == round((3600 + 7200 + 3000) / 3)   # completed prints only


def test_old_entries_map_from_completed_flag():
    assert stats.end_state_of({"completed": True}) == "complete"
    assert stats.end_state_of({"completed": False}) == "cancelled"
    assert stats.end_state_of({}) == "cancelled"
    assert stats.end_state_of({"end_state": "error", "completed": True}) == "error"


def test_period_is_inclusive_and_bare_dates_cover_the_whole_day():
    s = stats.compute_stats(H, "2026-10-05", "2026-10-05")
    assert s["prints"] == 2 and s["grams"] == 30.5
    s = stats.compute_stats(H, "2026-10-06", "2026-10-06")
    assert s["prints"] == 2                                  # 09:00 and 12:00 both inside
    assert stats.compute_stats(H, "2026-10-07", "2026-10-09")["prints"] == 0


def test_printer_filter():
    s = stats.compute_stats(H, printer_id="B")
    assert s["prints"] == 2 and s["grams"] == 25.5 and list(s["by_printer"]) == ["CC2"]


def test_grams_per_material_with_unknown_bucket():
    s = stats.compute_stats(H)
    assert s["by_material"] == {"PETG": 25.5, "PLA": 10.0, "Unknown": 11.5}


def test_per_printer_breakdown():
    s = stats.compute_stats(H)
    assert s["by_printer"]["CC1"] == {"prints": 4, "hours": round((3600 + 600 + 3000 + 500) / 3600, 2), "grams": 21.5}
    assert s["by_printer"]["CC2"]["prints"] == 2


def test_failure_reasons_use_cause_and_code():
    s = stats.compute_stats(H)
    assert s["failure_reasons"] == {"Hotend isn't heating (103)": 1, "user": 1, "cancelled": 1}


def test_daily_series_has_a_bar_for_every_day_including_empty_ones():
    s = stats.compute_stats(H, "2026-10-04", "2026-10-07")
    assert s["bucket"] == "day"
    assert [(b["key"], b["prints"], b["grams"]) for b in s["series"]] == [
        ("2026-10-04", 0, 0.0), ("2026-10-05", 2, 30.5), ("2026-10-06", 2, 6.5), ("2026-10-07", 0, 0.0)]


def test_longer_periods_switch_to_weeks_then_months():
    s = stats.compute_stats(H, "2026-08-01", "2026-10-31")
    assert s["bucket"] == "week" and s["series"][0]["key"] == "2026-07-27"      # a Monday
    assert sum(b["prints"] for b in s["series"]) == 6
    s = stats.compute_stats(H, "2025-01-01", "2026-12-31")
    assert s["bucket"] == "month"
    assert {b["key"]: b["prints"] for b in s["series"] if b["prints"]} == {"2026-09": 2, "2026-10": 4}
    assert len(s["series"]) == 24


def test_empty_history_is_fine():
    s = stats.compute_stats([])
    assert s["prints"] == 0 and s["success_rate"] is None and s["avg_print_s"] is None
    assert s["series"] == [] and s["by_material"] == {}
    assert stats.compute_stats([], "2026-10-01", "2026-10-03")["series"][0]["key"] == "2026-10-01"


def test_garbage_numbers_do_not_crash():
    s = stats.compute_stats([{"timestamp": "2026-10-01T00:00:00", "filament_g": "x", "print_time_s": None}])
    assert s["prints"] == 1 and s["grams"] == 0 and s["hours"] == 0


# ── CSV ──────────────────────────────────────────────────────────────────────

def test_csv_is_semicolon_bom_utf8_with_comma_decimals():
    data = stats.history_csv(H, "2026-10-05", "2026-10-05")
    assert data.startswith(b"\xef\xbb\xbf")
    lines = data.decode("utf-8-sig").split("\r\n")
    assert lines[0].split(";")[:4] == ["Date", "Started", "Reference", "Printer"]
    row = lines[2].split(";")
    assert row[3] == "CC2" and row[5] == "complete" and row[8] == "6,0"
    assert "20,5" in lines[2]


def test_csv_keeps_nordic_letters_and_neutralises_formulas():
    h = [{"timestamp": "2026-10-01T00:00:00", "printer_name": "Skriver æøå", "filename": "=HYPERLINK(\"x\")",
          "filament_g": 1, "filament_mm": 300, "print_time_s": 60, "end_state": "complete"}]
    text = stats.history_csv(h).decode("utf-8-sig")
    assert "Skriver æøå" in text and ";\"'=HYPERLINK" in text


def test_csv_quotes_semicolons_in_names():
    h = [{"timestamp": "2026-10-01T00:00:00", "printer_name": "A;B", "filename": "x", "end_state": "complete"}]
    assert '"A;B"' in stats.history_csv(h).decode("utf-8-sig")


# ── wiring ───────────────────────────────────────────────────────────────────

def test_endpoints_are_feature_gated():
    import http_handler
    assert features.FEATURES["statistics"].default is True
    assert http_handler.SPHandler._handle_stats._feature_gate == "statistics"
    assert http_handler.SPHandler._handle_history_csv._feature_gate == "statistics"


# ── reference numbers ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [("042", "042"), ("  A-17  ", "A-17"), (42, "42"), (None, ""), ("", ""),
                                          ("tab\there\nnew", "tabhere" "new")])
def test_clean_reference(raw, expected):
    assert stats.clean_reference(raw) == expected


@pytest.mark.parametrize("bad", ["x" * 41, ["a"], {"a": 1}, True])
def test_clean_reference_rejects(bad):
    with pytest.raises(ValueError):
        stats.clean_reference(bad)


def test_reference_is_in_the_csv():
    h = [{"timestamp": "2026-10-01T00:00:00", "printer_name": "P", "filename": "x", "end_state": "complete",
          "reference": "042"}]
    row = stats.history_csv(h).decode("utf-8-sig").split("\r\n")[1].split(";")
    assert row[2] == "042"


def test_reference_update_endpoint_is_gated_and_persists():
    import http_handler, persistence
    assert http_handler.SPHandler._handle_history_update._feature_gate == "statistics"
    persistence.append_history({"id": "a" * 32, "filename": "x"})
    assert persistence.update_history_entry("a" * 32, {"reference": stats.clean_reference(" 7 ")})
    assert persistence.load_history()[0]["reference"] == "7"


class _Req:
    """Just enough of SPHandler for the PATCH handler: path, body, _json."""
    def __init__(self, path, body):
        self.path, self._body, self.out = path, body, None

    def _read_body(self):
        import json
        return json.dumps(self._body).encode() if not isinstance(self._body, bytes) else self._body

    def _json(self, data, code=200):
        self.out = (code, data)


def _patch(entry_id, body):
    import http_handler
    r = _Req(f"/api/history/{entry_id}", body)
    r._apply_reference = lambda *a: http_handler.SPHandler._apply_reference(r, *a)
    http_handler.SPHandler._handle_history_update(r)
    return r.out


def test_patch_sets_clears_and_validates_reference():
    import persistence
    i = "b" * 32
    persistence.append_history({"id": i, "filename": "x"})
    assert _patch(i, {"reference": " 042 "}) == (200, {"ok": True, "reference": "042"})
    assert persistence.load_history()[0]["reference"] == "042"
    assert _patch(i, {"reference": ""})[1]["reference"] == ""
    assert _patch(i, {"reference": "x" * 41})[0] == 400
    assert _patch(i, {"filename": "hacked"})[0] == 400            # only the reference may be edited
    assert _patch(i, b"not json")[0] == 400
    assert _patch("c" * 32, {"reference": "1"})[0] == 404
    assert "hacked" not in str(persistence.load_history())


def test_patch_refused_when_statistics_feature_is_off():
    features.set_enabled("statistics", False)
    assert _patch("b" * 32, {"reference": "1"})[0] == 403
