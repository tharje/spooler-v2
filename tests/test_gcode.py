"""G-code metadata: parse slicer comment lines, cache by filename + sha256."""

import gcode

SAMPLE_TAIL = b"""G1 X10 Y10
; some print move
; filament used [mm] = 1234.5
; filament used [g] = 12.3
; filament_type = PLA
; filament_colour = #FFFFFF
; printer_model = centauri carbon 2
; estimated printing time (normal mode) = 1h 23m 45s
"""


def test_parse_gcode_meta_reads_known_keys():
    meta = gcode.parse_gcode_meta(b"", SAMPLE_TAIL)
    assert meta["filament_used_mm"] == 1234.5
    assert meta["filament_used_g"] == 12.3
    assert meta["filament_type"] == "PLA"
    assert meta["filament_colour"] == "#FFFFFF"
    assert meta["printer_model"] == "centauri carbon 2"
    assert meta["estimated_time"] == "1h 23m 45s"


def test_parse_gcode_meta_missing_keys_gives_partial_result():
    meta = gcode.parse_gcode_meta(b"; printer_model = cc1\n", b"")
    assert meta == {"printer_model": "cc1"}


def test_parse_gcode_meta_ignores_unrelated_comments_and_non_comment_lines():
    blob = b"G1 X0\n; just a note\n; printer_model = cc2\n"
    assert gcode.parse_gcode_meta(blob, b"") == {"printer_model": "cc2"}


def test_first_value_reduces_a_multimaterial_list():
    assert gcode.first_value({"filament_type": "PLA;PETG"}, "filament_type") == "PLA"
    assert gcode.first_value({}, "filament_type") is None


def test_meta_for_file_reads_head_and_tail_and_caches_by_name_and_hash(tmp_path):
    path = tmp_path / "x.gcode"
    path.write_bytes(b"; printer_model = cc2\n" + b"x" * 1000)
    meta = gcode.meta_for_file(path, "x.gcode")
    assert meta["printer_model"] == "cc2"
    cache = gcode._load_cache()
    assert len(cache) == 1

    # Same content again under the same name: cached, not re-parsed.
    meta2 = gcode.meta_for_file(path, "x.gcode")
    assert meta2 == meta
    assert len(gcode._load_cache()) == 1

    # Different content: a second cache entry, not a collision.
    path.write_bytes(b"; printer_model = cc1\n" + b"y" * 1000)
    meta3 = gcode.meta_for_file(path, "x.gcode")
    assert meta3["printer_model"] == "cc1"
    assert len(gcode._load_cache()) == 2


def test_meta_for_file_handles_a_file_smaller_than_the_head_window(tmp_path):
    path = tmp_path / "small.gcode"
    path.write_bytes(b"; printer_model = cc1\n")
    assert gcode.meta_for_file(path, "small.gcode")["printer_model"] == "cc1"
