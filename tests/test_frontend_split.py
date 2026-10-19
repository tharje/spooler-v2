"""The frontend is a handful of plain scripts sharing one global scope (no build
step). These checks keep the split honest: every file is loaded, in a fixed
order, and each parses."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PUBLIC = Path(__file__).resolve().parent.parent / "public"


def _loaded_scripts():
    html = (PUBLIC / "index.html").read_text()
    return re.findall(r'<script src="(app-[\w-]+\.js)\?v=\d+"></script>', html)


def test_every_app_file_is_loaded_exactly_once():
    on_disk = sorted(p.name for p in PUBLIC.glob("app-*.js"))
    loaded = _loaded_scripts()
    assert sorted(loaded) == on_disk
    assert len(loaded) == len(set(loaded))


def test_core_first_and_boot_last():
    loaded = _loaded_scripts()
    assert loaded[0] == "app-core.js" and loaded[-1] == "app-boot.js"


def test_no_stale_monolith():
    assert not (PUBLIC / "app.js").exists()


def test_each_file_parses_and_is_strict():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for name in _loaded_scripts():
        text = (PUBLIC / name).read_text()
        assert '"use strict";' in text, name
        out = subprocess.run([node, "--check", str(PUBLIC / name)], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f"{name}: {out.stderr}"
