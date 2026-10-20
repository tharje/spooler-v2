"""Picture collection: one folder per print, pictures and a note, and how the print ended."""

import asyncio
import json
from types import SimpleNamespace

import dataset


class FakePrinter:
    id = "p1"
    name = "My Printer/1"
    printer_type = "cc2"

    def __init__(self, status, jpeg=b"jpeg"):
        self.info = {"Status": status, "Filename": "benchy.gcode", "Progress": 42, "CurrentLayer": 7, "TotalLayer": 100}
        self.jpeg = jpeg
        self.state = "printing"

    def _decoded_printinfo(self):
        return self.info

    async def _camera_picture(self, max_age_s=20.0):
        return self.jpeg

    def to_dict(self):
        return {"state": self.state}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_a_print_gets_a_folder_with_pictures_and_an_ending(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset, "DATASET_DIR", tmp_path)
    monkeypatch.setattr(dataset, "_jobs", {})
    p = FakePrinter(status=2)
    run(dataset.collect_printer(p))
    run(dataset.collect_printer(p))
    (folder,) = [d for d in tmp_path.glob("*/*") if d.is_dir()]
    assert folder.parent.name == "My_Printer_1"                  # no characters that break a path
    assert len(list(folder.glob("*.jpg"))) >= 1
    lines = [json.loads(x) for x in (folder / "pictures.jsonl").read_text().splitlines()]
    assert len(lines) == 2 and lines[0]["progress"] == 42 and lines[0]["layer"] == 7
    meta = json.loads((folder / "job.json").read_text())
    assert meta["file"] == "benchy.gcode" and meta["outcome"] is None

    p.info["Status"] = 0                                          # the print is over
    p.state = "failed"
    run(dataset.collect_printer(p))
    meta = json.loads((folder / "job.json").read_text())
    assert meta["outcome"] == "failed" and meta["ended"]
    assert dataset._jobs == {}


def test_nothing_is_saved_while_not_printing_or_without_a_picture(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset, "DATASET_DIR", tmp_path)
    monkeypatch.setattr(dataset, "_jobs", {})
    run(dataset.collect_printer(FakePrinter(status=0)))
    run(dataset.collect_printer(FakePrinter(status=2, jpeg=None)))
    assert list(tmp_path.iterdir()) == []


def test_a_marked_picture_carries_the_label_and_the_first_failure_is_noted(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset, "DATASET_DIR", tmp_path)
    monkeypatch.setattr(dataset, "_jobs", {})
    monkeypatch.setattr(dataset, "grab_jpeg", lambda url: b"jpeg")
    p = FakePrinter(status=2)
    p.camera_url, p.connected = "http://camera", True
    dataset.label_now(p, "ok")
    dataset.label_now(p, "failed")
    (folder,) = [d for d in tmp_path.glob("*/*") if d.is_dir()]
    lines = [json.loads(x) for x in (folder / "pictures.jsonl").read_text().splitlines()]
    assert [x["label"] for x in lines] == ["ok", "failed"]
    assert sorted(f.name[9:] for f in folder.glob("*.jpg")) == ["failed.jpg", "ok.jpg"]
    assert json.loads((folder / "job.json").read_text())["failure_seen_at"]
    assert dataset._jobs["p1"]["n"] == 2


def test_label_is_refused_for_bad_label_or_idle_printer(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(dataset, "DATASET_DIR", tmp_path)
    monkeypatch.setattr(dataset, "_jobs", {})
    monkeypatch.setattr(dataset, "grab_jpeg", lambda url: b"jpeg")
    p = FakePrinter(status=2)
    p.camera_url, p.connected = "http://camera", True
    with pytest.raises(dataset.DatasetError):
        dataset.label_now(p, "maybe")
    p.info["Status"] = 0
    with pytest.raises(dataset.DatasetError):
        dataset.label_now(p, "ok")
