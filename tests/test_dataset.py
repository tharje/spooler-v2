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
    monkeypatch.setattr(dataset, "dataset_dir", lambda: tmp_path)
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
    monkeypatch.setattr(dataset, "dataset_dir", lambda: tmp_path)
    monkeypatch.setattr(dataset, "_jobs", {})
    run(dataset.collect_printer(FakePrinter(status=0)))
    run(dataset.collect_printer(FakePrinter(status=2, jpeg=None)))
    assert list(tmp_path.iterdir()) == []


def test_a_marked_picture_carries_the_label_and_the_first_failure_is_noted(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset, "dataset_dir", lambda: tmp_path)
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
    monkeypatch.setattr(dataset, "dataset_dir", lambda: tmp_path)
    monkeypatch.setattr(dataset, "_jobs", {})
    monkeypatch.setattr(dataset, "grab_jpeg", lambda url: b"jpeg")
    p = FakePrinter(status=2)
    p.camera_url, p.connected = "http://camera", True
    with pytest.raises(dataset.DatasetError):
        dataset.label_now(p, "maybe")
    p.info["Status"] = 0
    with pytest.raises(dataset.DatasetError):
        dataset.label_now(p, "ok")


def test_before_print_picture_starts_the_job_and_is_labelled(tmp_path, monkeypatch):
    import features
    monkeypatch.setattr(dataset, "dataset_dir", lambda: tmp_path)
    monkeypatch.setattr(dataset, "_jobs", {})
    features.set_enabled("dataset_collection", True)
    features.set_enabled("camera", True)
    p = FakePrinter(status=2)
    run(dataset.capture_before_print(p, p.info))
    (folder,) = [d for d in tmp_path.glob("*/*") if d.is_dir()]
    lines = [json.loads(x) for x in (folder / "pictures.jsonl").read_text().splitlines()]
    assert [x["label"] for x in lines] == ["before"]
    assert dataset._jobs["p1"]["n"] == 1


def test_before_print_picture_skipped_when_feature_off_or_no_folder(tmp_path, monkeypatch):
    import features
    monkeypatch.setattr(dataset, "dataset_dir", lambda: tmp_path)
    monkeypatch.setattr(dataset, "_jobs", {})
    p = FakePrinter(status=2)

    features.set_enabled("dataset_collection", False)
    features.set_enabled("camera", True)
    run(dataset.capture_before_print(p, p.info))
    assert dataset._jobs == {}

    features.set_enabled("dataset_collection", True)
    monkeypatch.setattr(dataset, "dataset_dir", lambda: None)
    run(dataset.capture_before_print(p, p.info))
    assert dataset._jobs == {}


def test_folder_is_chosen_by_the_user_and_must_be_a_full_path(monkeypatch):
    import pytest
    import config
    assert config.get("dataset.dir") == "" and dataset.dataset_dir() is None       # off until a folder is chosen
    config.set("dataset.dir", "/data/pictures/")
    assert dataset.dataset_dir().as_posix() == "/data/pictures"
    for bad in ("relative/path", "/a/../b"):
        with pytest.raises(config.ConfigError):
            config.set("dataset.dir", bad)
    config.set("dataset.dir", "")
    assert dataset.dataset_dir() is None
