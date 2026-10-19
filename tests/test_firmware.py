"""I4 / T5: firmware versions, the tested list, the yellow notice, change tracking."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import firmware
from printers.base import PrinterConnection
from printers.cc1 import CC1Connection
from printers.cc2 import CC2Connection
from printers.moonraker import MoonrakerConnection
from printers.prusa import PrusaConnection


@pytest.fixture
def tested(tmp_path, monkeypatch):
    f = tmp_path / "tested_firmware.json"

    def write(data):
        f.write_text(json.dumps(data))
    monkeypatch.setattr(firmware, "TESTED_FILE", f)
    write({"cc1": {"tested": ["V0.4.0-o", "V1.1.*"], "notes": {"V0.4.0-o": "OpenCentauri"}},
           "cc2": {"tested": ["02.01.00.00"], "notes": {}}})
    write.path = f
    return write


# ── matching ─────────────────────────────────────────────────────────────────

def test_exact_pattern_and_case(tested):
    assert firmware.is_tested("cc1", "V0.4.0-o") is True
    assert firmware.is_tested("cc1", "v0.4.0-O") is True                # case does not matter
    assert firmware.is_tested("cc1", "V1.1.42") is True                 # pattern V1.1.*
    assert firmware.is_tested("cc1", "V1.2.0") is False
    assert firmware.is_tested("cc1", "V0.4.1-o") is False               # exact means exact
    assert firmware.is_tested("cc2", "02.01.00.00") is True
    assert firmware.is_tested("cc2", "02.00.02.00") is False


def test_unknown_version_gives_no_verdict(tested):
    for v in (None, "", "   "):
        assert firmware.is_tested("cc1", v) is None


def test_printer_type_with_nothing_listed_is_untested_not_an_error(tested):
    assert firmware.is_tested("moonraker", "v0.12.0") is False
    assert firmware.is_tested("nonsense", "1") is False


def test_missing_or_broken_list_never_crashes(tested):
    tested.path.write_text("not json")
    assert firmware.is_tested("cc1", "V0.4.0-o") is False
    tested.path.unlink()
    assert firmware.is_tested("cc1", "V0.4.0-o") is False


def test_notes(tested):
    assert firmware.note_for("cc1", "v0.4.0-o") == "OpenCentauri"
    assert firmware.note_for("cc1", "V1.1.1") is None


def test_the_shipped_list_has_what_was_confirmed_as_tested():
    shipped = json.loads((Path(firmware.__file__).parent / "printers" / "tested_firmware.json").read_text())
    assert "V0.4.0-o" in shipped["cc1"]["tested"] and "02.01.00.00" in shipped["cc2"]["tested"]
    for t in ("cc1", "cc2", "moonraker", "prusa"):
        assert isinstance(shipped[t]["tested"], list) and isinstance(shipped[t]["notes"], dict)
        assert len(set(shipped[t]["tested"])) == len(shipped[t]["tested"])


# ── on the printer ───────────────────────────────────────────────────────────

def test_to_dict_flags_follow_the_list_removing_a_version_turns_the_notice_on(tested):
    p = CC1Connection("a", "10.0.0.1", "A")
    p.attrs = {"FirmwareVersion": "V0.4.0-o"}
    d = p.to_dict()
    assert d["firmware_version"] == "V0.4.0-o" and d["firmware_tested"] is True and d["firmware_note"] == "OpenCentauri"
    tested({"cc1": {"tested": [], "notes": {}}})                         # the version leaves the list
    d = p.to_dict()
    assert d["firmware_tested"] is False and d["firmware_note"] is None


def test_no_version_means_no_verdict_on_the_card(tested):
    p = PrusaConnection("a", "10.0.0.1", "A")
    d = p.to_dict()
    assert d["firmware_version"] is None and d["firmware_tested"] is None


def test_each_printer_type_reports_its_version(tested):
    c1 = CC1Connection("a", "10.0.0.1", "A"); c1.attrs = {"FirmwareVersion": "V0.4.0-o"}
    c2 = CC2Connection("b", "10.0.0.2", "B"); c2.attrs = {"FirmwareVersion": "02.01.00.00"}
    assert (c1.firmware_version, c2.firmware_version) == ("V0.4.0-o", "02.01.00.00")


def test_cc2_takes_the_version_from_method_1001_status(tested):
    p = CC2Connection("b", "10.0.0.2", "B")
    p._cc2_state["software_version"] = {"ota_version": "02.01.00.00"}
    p._apply_cc2_status()
    assert p.firmware_version == "02.01.00.00" and p.to_dict()["firmware_tested"] is True


@pytest.mark.asyncio
async def test_moonraker_reads_klipper_version(tested):
    p = MoonrakerConnection("m", "10.0.0.3", "M")

    async def req(method, path, body=None):
        assert path == "/printer/info"
        return {"result": {"software_version": "v0.12.0-1-gabc"}}
    p._req = req
    await p._fetch_version()
    assert p.firmware_version == "v0.12.0-1-gabc" and p.to_dict()["firmware_tested"] is False


@pytest.mark.asyncio
async def test_prusa_reads_firmware_and_survives_a_failure(tested):
    p = PrusaConnection("p", "10.0.0.4", "P")

    async def req(method, path, body=None):
        assert path == "/api/v1/info"
        return {"firmware": "6.1.0"}
    p._req = req
    await p._fetch_version()
    assert p.firmware_version == "6.1.0"

    q = PrusaConnection("q", "10.0.0.5", "Q")

    async def boom(*a, **k):
        raise OSError("down")
    q._req = boom
    await q._fetch_version()                                  # no exception, version stays unknown
    assert q.firmware_version is None


# ── noticing a change ────────────────────────────────────────────────────────

@pytest.fixture
def quiet(monkeypatch):
    emitted = []
    monkeypatch.setattr(PrinterConnection, "_emit",
                        lambda self, event, title, body="", priority="default", extra=None: emitted.append((event, title, body, extra)))
    return emitted


def test_first_sight_is_logged_not_announced(tested, quiet, capsys):
    p = CC1Connection("a", "10.0.0.1", "Bench")
    p.attrs = {"FirmwareVersion": "V9.9.9"}
    p._note_firmware()
    out = capsys.readouterr().out
    assert "Firmware V9.9.9" in out and "has not been tested" in out
    assert quiet == [] and firmware.last_seen("a") == "V9.9.9"
    p._note_firmware()                                         # called again on every update
    assert capsys.readouterr().out == ""


def test_a_change_is_logged_and_announced_and_survives_a_restart(tested, quiet, capsys):
    p = CC1Connection("a", "10.0.0.1", "Bench")
    p.attrs = {"FirmwareVersion": "V0.4.0-o"}
    p._note_firmware()
    assert capsys.readouterr().out.count("has not been tested") == 0       # tested: no complaint
    q = CC1Connection("a", "10.0.0.1", "Bench")                 # Spooler restarted; printer updated meanwhile
    q.attrs = {"FirmwareVersion": "V2.0.0"}
    q._note_firmware()
    assert "Firmware changed: V0.4.0-o -> V2.0.0" in capsys.readouterr().out
    [(event, title, body, extra)] = quiet
    assert event == "firmware_changed" and "V0.4.0-o → V2.0.0" in body and "not been tested" in body
    assert extra == {"from": "V0.4.0-o", "to": "V2.0.0", "tested": False}
    r = CC1Connection("a", "10.0.0.1", "Bench")
    r.attrs = {"FirmwareVersion": "V2.0.0"}
    r._note_firmware()
    assert len(quiet) == 1                                      # same version after another restart: silent


def test_the_notification_event_is_wired_to_a_setting():
    import notify
    assert notify.EVENT_SETTING["firmware_changed"] == "firmware"


# ── the notice on the card (runs the real frontend code under node) ──────────

PUBLIC = Path(__file__).resolve().parent.parent / "public"


class _AppJs:
    """The frontend is split over public/app-*.js; the blocks tested here are found in the whole."""
    @staticmethod
    def read_text():
        return "\n".join(p.read_text() for p in sorted(PUBLIC.glob("app-*.js")))


APP_JS = _AppJs()


def _badge(printer: dict) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    block = re.search(r"// firmware-badge:begin(.*?)// firmware-badge:end", APP_JS.read_text(), re.S).group(1)
    prelude = "function escAttr(s){return String(s).replace(/\"/g,'&quot;').replace(/'/g,'&#39;')}\n"
    out = subprocess.run([node, "-e", prelude + block + f"process.stdout.write(firmwareBadgeHtml({json.dumps(printer)}))"],
                         capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_notice_shows_only_for_untested_firmware():
    shown = _badge({"firmware_version": "V9", "firmware_tested": False})
    assert "untested firmware" in shown and "github.com/tharje/spooler-v2/issues" in shown
    assert "V9 has not been tested" in shown
    assert _badge({"firmware_version": "V0.4.0-o", "firmware_tested": True}) == ""
    assert _badge({"firmware_version": None, "firmware_tested": None}) == ""
    assert _badge({"firmware_version": "", "firmware_tested": False}) == ""


def test_notice_cannot_break_out_of_its_attributes_with_a_hostile_version_string():
    shown = _badge({"firmware_version": "x\" onmouseover=\"alert(1)' onfocus='y", "firmware_tested": False})
    # six attributes (class, href, target, rel, title, aria-label) -> exactly 12 quote characters survive,
    # i.e. the version's own quotes were all escaped and no new attribute was created
    assert shown.count('"') == 12
    assert re.findall(r' ([a-z-]+)="', shown) == ["class", "href", "target", "rel", "title", "aria-label"]
