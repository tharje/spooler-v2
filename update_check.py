"""
Tell the user when a newer Spooler release exists (opt-in).

When switched on, once a day Spooler asks GitHub for the latest release and
compares it with the running version. Nothing about the user or the printers
is sent: it is a plain GET with a User-Agent that names the Spooler version.
Pre-releases and drafts are ignored. No popup, nothing is updated for you.
"""

import asyncio
import json
import re
import time
import urllib.request

from features import is_enabled
from persistence import DATA_DIR, _atomic_write, current_version

RELEASE_URL = "https://api.github.com/repos/tharje/spooler-v2/releases/latest"
CHECK_EVERY_S = 24 * 3600
NOTES_MAX = 600
_FILE = DATA_DIR / "update_check.json"

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$")


def parse_version(text: str):
    """(major, minor, patch, prerelease-ids) or None when it is not a version number."""
    m = _SEMVER.match((text or "").strip())
    if not m:
        return None
    pre = m.group(4)
    ids = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in pre.split(".")) if pre else None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), ids


def is_newer(latest: str, current: str) -> bool:
    """True when `latest` is a later version than `current` (semver order: 2.3.0-beta.7 < 2.3.0)."""
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    if a[:3] != b[:3]:
        return a[:3] > b[:3]
    if a[3] == b[3]:
        return False
    if a[3] is None:
        return True            # a final release beats any pre-release of the same number
    if b[3] is None:
        return False
    return a[3] > b[3]


def _load() -> dict:
    try:
        data = json.loads(_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    _atomic_write(_FILE, json.dumps(data, indent=2))


def fetch_latest(timeout: float = 10.0) -> dict | None:
    """The latest published (non-draft, non-pre-release) release, or None."""
    req = urllib.request.Request(RELEASE_URL, headers={
        "User-Agent": f"Spooler/{current_version()}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read(1_000_000))
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    version = str(data.get("tag_name") or "").lstrip("v")
    if parse_version(version) is None:
        return None
    url = str(data.get("html_url") or "")
    if not url.startswith("https://github.com/"):
        url = "https://github.com/tharje/spooler-v2/releases"
    return {"version": version, "url": url, "notes": str(data.get("body") or "")[:NOTES_MAX]}


def check_now() -> None:
    """One check; remembers the answer. Quiet when GitHub cannot be reached."""
    data = _load()
    try:
        latest = fetch_latest()
    except Exception as e:
        print(f"[Update] Could not check for a new version ({type(e).__name__})")
        data["checked_at"] = time.time()
        _save(data)
        return
    data["checked_at"] = time.time()
    if latest:
        data.update({"latest": latest["version"], "url": latest["url"], "notes": latest["notes"]})
    _save(data)


def status() -> dict | None:
    """What the footer shows: {version, url, notes} when a newer release exists and was not skipped."""
    if not is_enabled("update_check"):
        return None
    data = _load()
    latest = data.get("latest")
    if not latest or latest == data.get("skipped") or not is_newer(latest, current_version()):
        return None
    return {"version": latest, "url": data.get("url") or "", "notes": data.get("notes") or ""}


def skip(version: str) -> None:
    data = _load()
    if version and version == data.get("latest"):
        data["skipped"] = version
        _save(data)


async def check_loop() -> None:
    while True:
        if is_enabled("update_check") and time.time() - float(_load().get("checked_at") or 0) >= CHECK_EVERY_S:
            await asyncio.get_running_loop().run_in_executor(None, check_now)
        await asyncio.sleep(3600)
