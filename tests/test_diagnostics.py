import sys

import diagnostics
import features
import state
from printers.cc2 import CC2Connection


def _printer():
    p = CC2Connection("pid", "192.168.10.130", "My Secret Printer", mainboard_id="F013B3B8WZZ9K11",
                      access_code="hunter2code")
    p.attrs = {"Model": "Centauri Carbon 2", "FirmwareVersion": "1.2.3",
               "Hostname": "elegoo-abcd", "MainboardID": "F013B3B8WZZ9K11"}
    p.status = {"note": "talks to 192.168.10.130 as hunter2code", "mac": "aa:bb:cc:dd:ee:ff"}
    return p


def test_redact_patterns():
    out = diagnostics.redact("ip 10.0.0.5 mac aa:bb:cc:dd:ee:ff tok abcdefghijklmnopqrstuvwxyz0123 "
                             'password="s3cret" cookie: abc', [])
    assert "10.0.0.5" not in out and "aa:bb" not in out and "abcdefghijklmnopqrstuvwxyz0123" not in out
    assert "s3cret" not in out and "cookie: <redacted>" in out


def test_report_has_no_known_secrets(monkeypatch):
    monkeypatch.setattr(state, "printers", {"pid": _printer()})
    diagnostics._buffer.clear()
    diagnostics._buffer.append("12:00:00 [Printer My Secret Printer] connected to 192.168.10.130 code hunter2code SN F013B3B8WZZ9K11")
    report = diagnostics.build_report()
    for secret in ("192.168.10.130", "hunter2code", "My Secret Printer", "F013B3B8WZZ9K11",
                   "elegoo-abcd", "aa:bb:cc:dd:ee:ff"):
        assert secret not in report
    assert "Centauri Carbon 2" in report and "1.2.3" in report and "Spooler version" in report


def test_log_capture_keeps_lines_and_passes_through(capsys):
    diagnostics._buffer.clear()
    real = sys.stdout
    try:
        sys.stdout = diagnostics._Tee(real)
        print("hello")
        print("part", end="")
        print("ial")
    finally:
        sys.stdout = real
    lines = diagnostics.recent_log_lines()
    assert lines[0].endswith("hello") and lines[1].endswith("partial")


def test_feature_registered_and_endpoint_gated():
    import http_handler
    assert features.FEATURES["report_problem"].default is True
    assert http_handler.SPHandler._handle_diagnostics._feature_gate == "report_problem"
