"""New-version notice: version order, what counts as a release, and that nothing is fetched when off."""

import json

import pytest

import features
import update_check as uc


@pytest.mark.parametrize("latest,current,newer", [
    ("2.4.0", "2.3.0", True),
    ("2.3.1", "2.3.0", True),
    ("2.3.0", "2.3.0", False),
    ("2.3.0", "2.3.1", False),
    ("2.10.0", "2.9.0", True),                      # numbers, not text
    ("2.3.0", "2.3.0-beta.7", True),                # the final release beats its own beta
    ("2.3.0-beta.7", "2.3.0", False),
    ("2.3.0-beta.10", "2.3.0-beta.9", True),
    ("v2.4.0", "2.3.0", True),
    ("garbage", "2.3.0", False),
    ("2.4.0", "unknown", False),
])
def test_version_order(latest, current, newer):
    assert uc.is_newer(latest, current) is newer


class _Reply:
    def __init__(self, data): self.data = json.dumps(data).encode()
    def read(self, n=-1): return self.data
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _release(**over):
    base = {"tag_name": "v2.4.0", "html_url": "https://github.com/tharje/spooler-v2/releases/tag/v2.4.0",
            "body": "Notes", "draft": False, "prerelease": False}
    return {**base, **over}


def test_fetch_reads_a_release_and_ignores_drafts_and_prereleases(monkeypatch):
    seen = {}
    def fake(req, timeout=0):
        seen["ua"] = req.get_header("User-agent")
        return _Reply(seen["data"])
    monkeypatch.setattr(uc.urllib.request, "urlopen", fake)
    seen["data"] = _release()
    assert uc.fetch_latest()["version"] == "2.4.0"
    assert seen["ua"].startswith("Spooler/")                                    # and nothing else identifying
    for over in ({"draft": True}, {"prerelease": True}, {"tag_name": "nightly"}):
        seen["data"] = _release(**over)
        assert uc.fetch_latest() is None
    seen["data"] = _release(html_url="https://evil.example/x")
    assert uc.fetch_latest()["url"].startswith("https://github.com/")           # a foreign link is never shown


def test_notice_only_when_on_newer_and_not_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(uc, "_FILE", tmp_path / "update_check.json")
    monkeypatch.setattr(uc, "current_version", lambda: "2.3.0")
    monkeypatch.setattr(uc.urllib.request, "urlopen", lambda req, timeout=0: _Reply(_release()))
    features.set_enabled("update_check", False)
    uc.check_now()
    assert uc.status() is None                                                  # off: no notice
    features.set_enabled("update_check", True)
    assert uc.status()["version"] == "2.4.0"
    uc.skip("2.4.0")
    assert uc.status() is None                                                  # skipped version stays hidden
    monkeypatch.setattr(uc.urllib.request, "urlopen", lambda req, timeout=0: _Reply(_release(tag_name="v2.5.0")))
    uc.check_now()
    assert uc.status()["version"] == "2.5.0"                                    # a newer one shows again
    monkeypatch.setattr(uc, "current_version", lambda: "2.5.0")
    assert uc.status() is None


def test_github_down_is_quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(uc, "_FILE", tmp_path / "update_check.json")
    def boom(req, timeout=0): raise OSError("down")
    monkeypatch.setattr(uc.urllib.request, "urlopen", boom)
    uc.check_now()                                                              # no exception
    assert uc._load()["checked_at"] > 0
