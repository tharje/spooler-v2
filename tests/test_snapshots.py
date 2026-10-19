"""T13: one picture per finished print -- storage, trimming, orphan cleanup, capture."""

import asyncio
import uuid

import pytest

import features
import persistence
import printers.base as base
from printers.base import PrinterConnection

JPEG = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"


def _id():
    return uuid.uuid4().hex


def test_snapshot_path_accepts_only_plain_history_ids(tmp_path):
    good = _id()
    assert persistence.snapshot_path(good) == tmp_path / "snapshots" / f"{good}.jpg"
    for bad in ("../x", "a/b", "..", "", "ABC", good + "0", good[:-1], None, 5):
        assert persistence.snapshot_path(bad) is None


def test_save_and_delete_round_trip(tmp_path):
    i = _id()
    assert persistence.save_snapshot(i, JPEG)
    assert persistence.snapshot_path(i).read_bytes() == JPEG
    persistence.delete_snapshots([i, "not-an-id", None])
    assert not persistence.snapshot_path(i).exists()
    assert not persistence.save_snapshot("../evil", JPEG) and not persistence.save_snapshot(i, b"")


def test_trimming_history_deletes_the_pictures_that_fall_off(monkeypatch):
    monkeypatch.setattr(persistence, "HISTORY_MAX_ENTRIES", 2)
    ids = [_id() for _ in range(3)]
    for i in ids[:2]:
        persistence.save_snapshot(i, JPEG)
        persistence.append_history({"id": i, "snapshot": True})
    persistence.append_history({"id": ids[2]})          # pushes ids[0] out
    assert not persistence.snapshot_path(ids[0]).exists()
    assert persistence.snapshot_path(ids[1]).exists()


def test_orphans_removed_at_startup_but_kept_pictures_stay(tmp_path):
    keep, gone = _id(), _id()
    persistence.append_history({"id": keep, "snapshot": True})
    persistence.save_snapshot(keep, JPEG)
    persistence.save_snapshot(gone, JPEG)
    (tmp_path / "snapshots" / ".leftover.tmp").write_bytes(b"x")
    assert persistence.cleanup_orphan_snapshots() == 2
    assert persistence.snapshot_path(keep).exists() and not persistence.snapshot_path(gone).exists()
    assert persistence.cleanup_orphan_snapshots() == 0


def test_cleanup_without_folder_is_a_noop():
    assert persistence.cleanup_orphan_snapshots() == 0


def test_update_history_entry():
    i = _id()
    persistence.append_history({"id": i, "filename": "a.gcode"})
    assert persistence.update_history_entry(i, {"snapshot": True})
    assert persistence.load_history()[0]["snapshot"] is True
    assert not persistence.update_history_entry(_id(), {"snapshot": True})


def test_atomic_write_accepts_bytes(tmp_path):
    persistence._atomic_write(tmp_path / "b.bin", b"\x00\xff")
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\xff"


def test_feature_registered_and_endpoint_gated():
    assert features.FEATURES["print_snapshot"].requires == ("camera",)
    import http_handler
    assert http_handler.SPHandler._handle_snapshot._feature_gate == "print_snapshot"


# ── capture ──────────────────────────────────────────────────────────────────

@pytest.fixture
def printer(monkeypatch):
    p = PrinterConnection("pid1", "10.0.0.5", "Bench")
    p.connected = True
    p.camera_url = "http://cam/"
    sent = []

    async def fake_broadcast(msg):
        sent.append(msg)
    monkeypatch.setattr(base.state, "broadcast_to_browsers", fake_broadcast)
    p.broadcasts = sent
    return p


@pytest.mark.asyncio
async def test_picture_saved_and_history_flagged(printer, monkeypatch):
    monkeypatch.setattr(base, "grab_jpeg", lambda url: JPEG)
    i = _id()
    persistence.append_history({"id": i})
    await printer._save_print_picture(i)
    assert persistence.snapshot_path(i).read_bytes() == JPEG
    assert persistence.load_history()[0]["snapshot"] is True
    assert {"type": "history_snapshot", "id": i} in printer.broadcasts


@pytest.mark.asyncio
async def test_no_camera_or_failed_grab_leaves_no_trace(printer, monkeypatch):
    i = _id()
    persistence.append_history({"id": i})
    printer.camera_url = None
    await printer._save_print_picture(i)
    printer.camera_url = "http://cam/"
    monkeypatch.setattr(base, "grab_jpeg", lambda url: None)
    await printer._save_print_picture(i)
    assert not persistence.snapshot_path(i).exists()
    assert "snapshot" not in persistence.load_history()[0] and printer.broadcasts == []


@pytest.mark.asyncio
async def test_offline_printer_takes_no_picture(printer, monkeypatch):
    called = []
    monkeypatch.setattr(base, "grab_jpeg", lambda url: called.append(url) or JPEG)
    printer.connected = False
    await printer._save_print_picture(_id())
    assert called == []


@pytest.mark.asyncio
async def test_feature_off_takes_no_picture(printer, monkeypatch):
    called = []
    monkeypatch.setattr(base, "grab_jpeg", lambda url: called.append(url) or JPEG)
    features.set_enabled("print_snapshot", False)
    await printer._save_print_picture(_id())
    assert called == []
    features.set_enabled("print_snapshot", True)
    features.set_enabled("camera", False)      # print_snapshot requires camera
    await printer._save_print_picture(_id())
    assert called == []


@pytest.mark.asyncio
async def test_entry_trimmed_before_picture_arrives_leaves_no_orphan(printer, monkeypatch):
    monkeypatch.setattr(base, "grab_jpeg", lambda url: JPEG)
    i = _id()                                  # never added to history
    await printer._save_print_picture(i)
    assert not persistence.snapshot_path(i).exists()


@pytest.mark.asyncio
async def test_one_grab_is_shared_between_picture_and_notification(printer, monkeypatch):
    calls = []
    monkeypatch.setattr(base, "grab_jpeg", lambda url: calls.append(1) or JPEG)
    a = await printer._camera_picture()
    b = await printer._camera_picture()
    assert a == b == JPEG and len(calls) == 1
