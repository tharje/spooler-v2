"""
Shared test fixtures.

Tests must never touch the real DATA_DIR, Spoolman, or the network. Every test
gets its persistence files redirected into a fresh tmp_path automatically.
"""

import pytest

import backup
import config
import features
import persistence
import push


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point persistence's (and backup's, and features') file constants at a
    throwaway directory per test.

    persistence.py resolves DATA_DIR once at import time, so later os.environ
    changes don't retarget it — patch the already-bound Path objects directly
    instead of relying on the env var. backup.py and features.py each import
    their own DATA_DIR-derived reference from persistence at import time too,
    so they need the same treatment or they'd keep pointing at the real one.
    """
    monkeypatch.setattr(persistence, "DATA_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PRINTERS_FILE", tmp_path / "printers.json")
    monkeypatch.setattr(persistence, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(persistence, "TRAY_MAP_FILE", tmp_path / "tray_map.json")
    monkeypatch.setattr(backup, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup, "_VERSION_MARKER", tmp_path / ".last_version")
    monkeypatch.setattr(backup, "_DAILY_MARKER", tmp_path / ".last_daily_backup")
    monkeypatch.setattr(push, "NOTIF_SETTINGS_FILE", tmp_path / "notification_settings.json")
    monkeypatch.setattr(features, "FEATURES_FILE", tmp_path / "features.json")
    # _FORCE_DISABLED and _on_change_callbacks are plain module-level globals,
    # not file-backed -- without resetting them, a callback registered (or a
    # force-disable set via monkeypatch) in one test would leak into the next.
    monkeypatch.setattr(features, "_FORCE_DISABLED", set())
    monkeypatch.setattr(features, "_on_change_callbacks", {})
    monkeypatch.setattr(config, "INTEGRATIONS_FILE", tmp_path / "integrations.json")
    monkeypatch.setattr(config, "LOCK_CONFIG", False)
    monkeypatch.setattr(config, "_on_change", [])
    monkeypatch.setattr(config, "_last_test_results", {})
    yield tmp_path
