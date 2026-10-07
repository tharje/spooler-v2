"""Which image tags get published (scripts/docker-tags.sh), and that the
workflow uses it. A beta must never become :latest."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "docker-tags.sh"
IMG = "ghcr.io/tharje/spooler-v2"
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="bash is not installed")


def tags(ref, name, changelog, sha="abc123"):
    r = subprocess.run(["bash", str(SCRIPT), ref, name, sha, changelog], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def test_push_to_main_keeps_publishing_latest_and_the_sha():
    assert tags("refs/heads/main", "main", "2.3.0-beta") == (0, f"{IMG}:latest,{IMG}:abc123", "")


def test_release_tag_gets_its_version_and_latest():
    assert tags("refs/tags/v2.3.0", "v2.3.0", "2.3.0")[:2] == (0, f"{IMG}:2.3.0,{IMG}:latest")


@pytest.mark.parametrize("name,changelog,version", [
    ("v2.3.0-beta.1", "2.3.0-beta", "2.3.0-beta.1"),
    ("v2.3.0-beta", "2.3.0-beta", "2.3.0-beta"),
    ("v2.3.0-rc.2", "2.3.0-rc", "2.3.0-rc.2"),
])
def test_prerelease_tags_get_beta_and_never_latest(name, changelog, version):
    code, out, _ = tags(f"refs/tags/{name}", name, changelog)
    assert (code, out) == (0, f"{IMG}:{version},{IMG}:beta")
    assert ":latest" not in out


@pytest.mark.parametrize("name", ["v2.3", "v2.3.0.1", "vfoo", "v2.3.0-", "v2.3.0 beta", "v2.3.0-beta;rm", "v2.3.0-beta$(x)", "v2.3.0/../x"])
def test_malformed_tags_publish_nothing(name):
    code, out, err = tags(f"refs/tags/{name}", name, "2.3.0-beta")
    assert code == 1 and out == "" and "not of the form" in err


def test_tag_that_disagrees_with_the_changelog_publishes_nothing():
    code, out, err = tags("refs/tags/v2.4.0", "v2.4.0", "2.3.0-beta")
    assert code == 1 and out == "" and "does not match" in err
    code, out, err = tags("refs/tags/v2.3.0", "v2.3.0", "2.3.0-beta")        # final tag while the app still says beta
    assert code == 1 and "does not match" in err
    assert tags("refs/tags/v2.3.01", "v2.3.01", "2.3.0")[0] == 1               # prefix match must be on a dot


def test_other_refs_publish_nothing():
    for ref, name in [("refs/heads/dev", "dev"), ("refs/pull/5/merge", "5/merge"), ("refs/tags/release-1", "release-1")]:
        assert tags(ref, name, "2.3.0")[0] == 1


def test_the_workflow_uses_the_script_and_triggers_on_tags_and_main_only():
    wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
    assert 'tags: ["v*"]' in wf and "branches: [main]" in wf
    assert "scripts/docker-tags.sh" in wf and "steps.tags.outputs.tags" in wf
    assert "spooler-v2:latest" not in wf                       # no hard-coded :latest left; tags come only from the script
    assert (ROOT / "scripts" / "docker-tags.sh").stat().st_mode & 0o111


def test_changelog_top_entry_is_what_the_tag_check_reads():
    top = json.loads((ROOT / "public" / "changelog.json").read_text())[0]["version"]
    assert isinstance(top, str) and top
