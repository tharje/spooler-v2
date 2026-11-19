"""The card's percentage: the printer's own figure when it sends one (issue #56)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parent.parent / "public" / "app-core.js").read_text()


def _progress(pi):
    """Run getProgress from public/app-core.js under node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    fn = re.search(r"function getProgress\(printer\) \{.*?\n\}\n", SRC, re.S).group(0)
    script = fn + f"console.log(JSON.stringify(getProgress({{status: {{PrintInfo: {json.dumps(pi)}}}}})));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_printers_own_percentage_wins_over_layers():
    # CC2 sends both: layer 40 of 133 is 30 %, while the printer says 55 %
    assert _progress({"CurrentTicks": 55, "TotalTicks": 100, "CurrentLayer": 40, "TotalLayer": 133}) == 55


def test_the_percentage_is_kept_within_0_and_100():
    assert _progress({"CurrentTicks": 104, "TotalTicks": 100}) == 100
    assert _progress({"CurrentTicks": -3, "TotalTicks": 100}) == 0


def test_cc1_ticks_are_seconds_so_layers_still_decide():
    assert _progress({"CurrentTicks": 1200, "TotalTicks": 3600, "CurrentLayer": 50, "TotalLayer": 100}) == 50


def test_cc1_without_layers_uses_the_time():
    assert _progress({"CurrentTicks": 900, "TotalTicks": 3600, "TotalLayer": 0}) == 25


def test_nothing_to_go_on():
    assert _progress({"TotalTicks": 0, "TotalLayer": 0}) is None
