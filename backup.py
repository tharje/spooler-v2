"""
Backup and restore: zip up DATA_DIR's own JSON files, validate and restore
from a previously-created zip.

Deliberately excludes (per the project's own backup policy): raw/ (debug
captures, can contain serials/access codes and is purely diagnostic),
cert.pem/key.pem (TLS material -- regenerated automatically if missing),
uploads/ and gcode_meta.json (don't exist yet -- T9), snapshots/ (don't
exist yet -- T13), and anything not explicitly listed below. Only files this
module already knows about by exact name are ever read from or written into
DATA_DIR -- this is also what makes restore safe against zip-slip: a zip
member's own path is never used as a filesystem path, only as a lookup key
into this fixed allowlist.
"""

import hashlib
import json
import os
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import config
from features import is_enabled
from persistence import DATA_DIR, _atomic_write, current_version

BACKUP_DIR = DATA_DIR / "backups"

# Every file backup.py will ever read from or write into DATA_DIR. Files that
# don't exist (e.g. vapid_keys.json when pywebpush isn't installed) are
# silently skipped when creating a backup, and silently left untouched on
# restore if the backup doesn't contain them either.
BACKUP_FILES = [
    "printers.json",
    "history.json",
    "tray_map.json",
    "notification_settings.json",
    "push_subscriptions.json",
    "vapid_keys.json",
    "auth.json",
]

# Fields inside specific files to redact when a backup is created without
# secrets. Printer access codes are the only credential-shaped data in the
# files above today (auth.json/vapid_keys.json are this instance's own
# identity, not third-party integration credentials, so they're always
# included in full -- see the module docstring in the project's roadmap notes
# for the reasoning).
_SECRET_FIELDS = {"printers.json": ("access_code",)}

MANIFEST_NAME = "manifest.json"

_backup_lock = threading.Lock()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact(filename: str, raw: bytes) -> bytes:
    fields = _SECRET_FIELDS.get(filename)
    if not fields:
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                for f in fields:
                    if f in entry:
                        entry[f] = ""
    return json.dumps(data, indent=2).encode()


def create_backup_zip(dest_path: Path, include_secrets: bool = False) -> dict:
    """Write a backup zip to dest_path. Returns the manifest dict written.

    Streamed to disk via a temp file and zipfile's own chunked I/O rather
    than building the archive in memory -- today's files are tiny, but this
    stays correct if large optional content (e.g. future print snapshots)
    is ever added to BACKUP_FILES.
    """
    manifest = {
        "spooler_version": current_version(),
        "created_at":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "includes_secrets": include_secrets,
        "files": [],
    }
    tmp_fd, tmp_name = tempfile.mkstemp(dir=dest_path.parent, prefix=".backup.", suffix=".zip.tmp")
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in BACKUP_FILES:
                path = DATA_DIR / name
                if not path.exists():
                    continue
                raw = path.read_bytes()
                if not include_secrets:
                    raw = _redact(name, raw)
                zf.writestr(name, raw)
                manifest["files"].append({"name": name, "sha256": _sha256(raw)})
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        os.replace(tmp_name, dest_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return manifest


class RestoreError(Exception):
    pass


def _parse_version(v: str) -> tuple:
    try:
        return tuple(int(p) for p in v.split("."))
    except Exception:
        return (0,)


def validate_backup_zip(zip_path: Path) -> dict:
    """Validate a backup zip without writing anything. Returns the manifest
    dict on success, raises RestoreError with a human-readable reason otherwise.
    """
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile:
        raise RestoreError("Not a valid zip file.")

    with zf:
        names = set(zf.namelist())
        if MANIFEST_NAME not in names:
            raise RestoreError("Missing manifest.json -- not a Spooler backup.")
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME))
        except Exception:
            raise RestoreError("manifest.json is not valid JSON.")

        backup_version = manifest.get("spooler_version", "")
        if _parse_version(backup_version) > _parse_version(current_version()):
            raise RestoreError(
                f"This backup is from a newer Spooler version ({backup_version}) "
                f"than is currently running ({current_version()}). Update Spooler first."
            )

        declared = manifest.get("files")
        if not isinstance(declared, list):
            raise RestoreError("manifest.json is missing its file list.")

        # Allowlist in both directions: every declared file must be one we
        # recognise, and every actual zip member (besides the manifest
        # itself) must have been declared -- an unexpected extra member is
        # treated as suspicious rather than silently ignored. Zip member
        # names are never used as filesystem paths; only as lookup keys
        # against BACKUP_FILES, so directory-traversal names (e.g.
        # "../../etc/cron.d/x") can't escape DATA_DIR regardless.
        declared_names = set()
        for entry in declared:
            name = entry.get("name") if isinstance(entry, dict) else None
            if name not in BACKUP_FILES:
                raise RestoreError(f"Unrecognised file in backup: {name!r}")
            declared_names.add(name)

        extra = names - declared_names - {MANIFEST_NAME}
        if extra:
            raise RestoreError(f"Unexpected extra content in backup: {sorted(extra)}")

        for entry in declared:
            name = entry["name"]
            if name not in names:
                raise RestoreError(f"Backup is missing declared file: {name}")
            raw = zf.read(name)
            if _sha256(raw) != entry.get("sha256"):
                raise RestoreError(f"Checksum mismatch for {name} -- backup may be corrupt.")

    return manifest


def restore_from_zip(zip_path: Path) -> dict:
    """Validate, then restore. Takes its own automatic backup of the current
    state first, so a restore is itself reversible. Returns the manifest of
    the restored backup.
    """
    manifest = validate_backup_zip(zip_path)

    with _backup_lock:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        pre_restore = BACKUP_DIR / f"pre-restore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.zip"
        try:
            create_backup_zip(pre_restore, include_secrets=True)
        except Exception as e:
            print(f"[Backup] Could not snapshot current state before restore: {e}")

        with zipfile.ZipFile(zip_path, "r") as zf:
            for entry in manifest["files"]:
                name = entry["name"]
                raw = zf.read(name)
                # Every file here is this instance's own credentials/identity
                # (auth hash, printer access codes, VAPID private key, ...) --
                # restore must not leave any of them world-readable, same as
                # when they're first written outside of a restore.
                _atomic_write(DATA_DIR / name, raw.decode("utf-8"), mode=0o600)

    return manifest


def list_auto_backups() -> list:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob("*.zip"), reverse=True):
        try:
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "modified": st.st_mtime})
        except OSError:
            continue
    return out


def cleanup_old_backups(keep: int) -> None:
    # Sort by modification time, not filename -- different backup kinds have
    # different filename prefixes ("daily-", "pre-upgrade-", "pre-restore-"),
    # so sorting by name would group by kind instead of true chronological
    # order and could delete a recent backup while keeping an older one.
    files = sorted(BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def make_automatic_backup(prefix: str) -> Path | None:
    """Create backups/<prefix>-<timestamp>.zip. Returns the path, or None on
    failure (best-effort -- a backup failure must never block startup)."""
    try:
        with _backup_lock:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            dest = BACKUP_DIR / f"{prefix}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.zip"
            create_backup_zip(dest, include_secrets=True)
            return dest
    except Exception as e:
        print(f"[Backup] Automatic backup failed: {e}")
        return None


# ── Automatic backup scheduling ──────────────────────────────────────────────
#
# Retention count has no settings-UI field yet, so it follows the same
# pattern as every other current env-var-only toggle in this project
# (AUTH_ENABLED, etc.). The interval IS user-configurable, via
# config.py's "backup.interval_days" (Settings -> Backup) -- 0 disables it.
AUTO_BACKUP_KEEP = int(os.getenv("SPOOLER_BACKUP_KEEP", "7"))


def backup_interval_seconds() -> int:
    # Read live (not a module constant) so a change in Settings -> Backup
    # takes effect on the next hourly check, no restart needed.
    return config.get("backup.interval_days") * 24 * 3600


_VERSION_MARKER = DATA_DIR / ".last_version"
_DAILY_MARKER   = DATA_DIR / ".last_daily_backup"


def check_startup_backup() -> None:
    """Back up before any migration runs, if the Spooler version differs from
    the last recorded startup. Skipped on a brand-new install -- with no
    prior version recorded there's nothing meaningful to protect yet.

    Also skipped entirely while the "backup" feature is off -- deliberately
    does NOT update _VERSION_MARKER in that case either, so turning the
    feature back on later still catches the version change it missed instead
    of silently treating it as already-seen.
    """
    if not is_enabled("backup"):
        return
    current = current_version()
    try:
        last = _VERSION_MARKER.read_text().strip()
    except FileNotFoundError:
        last = None
    if last is not None and last != current:
        print(f"[Backup] Version changed ({last} -> {current}) — backing up before any migration runs.")
        if make_automatic_backup("pre-upgrade"):
            cleanup_old_backups(AUTO_BACKUP_KEEP)
    try:
        _VERSION_MARKER.write_text(current)
    except OSError:
        pass


def maybe_daily_backup() -> None:
    # Both checked every call (not just once) so toggling "backup" off, or
    # changing the interval, in Settings takes effect immediately -- no
    # restart needed.
    interval_s = backup_interval_seconds()
    if interval_s <= 0 or not is_enabled("backup"):
        return
    now = time.time()
    try:
        last = float(_DAILY_MARKER.read_text().strip())
    except (FileNotFoundError, ValueError):
        last = 0.0
    if now - last < interval_s:
        return
    if make_automatic_backup("daily"):
        cleanup_old_backups(AUTO_BACKUP_KEEP)
    try:
        _DAILY_MARKER.write_text(str(now))
    except OSError:
        pass


async def daily_backup_loop() -> None:
    """Checks hourly so a long-running process doesn't need to sleep a full
    24h to notice the window has passed; maybe_daily_backup() itself only
    actually backs up once that long has elapsed since the last one."""
    import asyncio
    while True:
        maybe_daily_backup()
        await asyncio.sleep(3600)
