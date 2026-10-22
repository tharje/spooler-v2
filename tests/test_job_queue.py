"""T20: the print queue -- add/list/update/delete, assignment to a matching
idle printer, and confirm_plate_cleared's upload+start."""

import asyncio

import pytest

import features
import job_queue
import state


class FakePrinter:
    def __init__(self, id, printer_type="cc2", state_="idle", connected=True, tray_spool=None):
        self.id = id
        self.name = f"Printer {id}"
        self.printer_type = printer_type
        self.connected = connected
        self._state = state_
        self._tray = tray_spool
        self.uploaded = None
        self.upload_result = True

    def to_dict(self):
        return {"state": self._state}

    def _tray_spool(self):
        return self._tray

    async def upload_file(self, local_path, remote_name, start_after=False):
        self.uploaded = (local_path, remote_name, start_after)
        return self.upload_result


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(state, "printers", {})
    features.set_enabled("job_queue", True)
    features.set_enabled("file_upload", True)
    yield


def _gcode(tmp_path, name="a.gcode", printer_model=None):
    path = tmp_path / name
    body = f"; printer_model = {printer_model}\n".encode() if printer_model else b""
    path.write_bytes(body + b"x" * 100)
    return path


def test_add_job_stores_file_and_requirements(tmp_path):
    job = job_queue.add_job(_gcode(tmp_path), "a.gcode",
                             {"printer_type": "cc2", "material": "PLA", "quantity": 2, "priority": 5})
    assert job["status"] == "queued" and job["quantity"] == 2 and job["priority"] == 5
    assert job["material"] == "PLA"
    assert (job_queue.QUEUE_DIR / f"{job['id']}.gcode").exists()
    assert job_queue.list_jobs() == [job]


def test_add_job_rejects_unknown_specific_printer(tmp_path):
    with pytest.raises(job_queue.QueueError):
        job_queue.add_job(_gcode(tmp_path), "a.gcode", {"printer_id": "does-not-exist"})


def test_add_job_reads_the_gcode_printer_model(tmp_path):
    job = job_queue.add_job(_gcode(tmp_path, printer_model="centauri carbon 2"), "a.gcode", {})
    assert job["gcode_printer_model"] == "centauri carbon 2"


def test_list_jobs_sorts_active_first_by_priority_then_age(tmp_path):
    low = job_queue.add_job(_gcode(tmp_path, "low.gcode"), "low.gcode", {"priority": 1})
    high = job_queue.add_job(_gcode(tmp_path, "high.gcode"), "high.gcode", {"priority": 9})
    jobs = job_queue.list_jobs()
    assert [j["id"] for j in jobs] == [high["id"], low["id"]]


def test_update_job_changes_priority_and_quantity(tmp_path):
    job = job_queue.add_job(_gcode(tmp_path), "a.gcode", {})
    updated = job_queue.update_job(job["id"], {"priority": 7, "quantity": 3})
    assert updated["priority"] == 7 and updated["quantity"] == 3


def test_update_job_can_cancel_a_queued_job(tmp_path):
    job = job_queue.add_job(_gcode(tmp_path), "a.gcode", {})
    cancelled = job_queue.update_job(job["id"], {"status": "cancelled"})
    assert cancelled["status"] == "cancelled"


def test_update_job_refuses_unknown_job():
    with pytest.raises(job_queue.QueueError):
        job_queue.update_job("nope", {"priority": 1})


def test_delete_job_removes_record_and_file(tmp_path):
    job = job_queue.add_job(_gcode(tmp_path), "a.gcode", {})
    path = job_queue.QUEUE_DIR / f"{job['id']}.gcode"
    assert path.exists()
    job_queue.delete_job(job["id"])
    assert job_queue.list_jobs() == [] and not path.exists()
    with pytest.raises(job_queue.QueueError):
        job_queue.delete_job(job["id"])


def test_assign_ready_jobs_matches_idle_printer_by_type(tmp_path):
    state.printers["p1"] = FakePrinter("p1", printer_type="cc2")
    job_queue.add_job(_gcode(tmp_path), "a.gcode", {"printer_type": "cc2"})
    job_queue.assign_ready_jobs()
    jobs = job_queue.list_jobs()
    assert jobs[0]["status"] == "assigned" and jobs[0]["assigned_printer_id"] == "p1"


def test_assign_ready_jobs_skips_disconnected_or_busy_printers(tmp_path):
    state.printers["p1"] = FakePrinter("p1", connected=False)
    state.printers["p2"] = FakePrinter("p2", state_="printing")
    job_queue.add_job(_gcode(tmp_path), "a.gcode", {})
    job_queue.assign_ready_jobs()
    assert job_queue.list_jobs()[0]["status"] == "queued"


def test_assign_ready_jobs_does_nothing_when_feature_off(tmp_path):
    features.set_enabled("job_queue", False)
    state.printers["p1"] = FakePrinter("p1")
    job_queue.add_job(_gcode(tmp_path), "a.gcode", {})
    job_queue.assign_ready_jobs()
    assert job_queue.list_jobs()[0]["status"] == "queued"


def test_assign_ready_jobs_prefers_material_match(tmp_path, monkeypatch):
    monkeypatch.setattr(job_queue, "_spool_material", lambda p: {"p1": "PETG", "p2": "PLA"}.get(p.id))
    state.printers["p1"] = FakePrinter("p1")
    state.printers["p2"] = FakePrinter("p2")
    job = job_queue.add_job(_gcode(tmp_path), "a.gcode", {"material": "PLA"})
    job_queue.assign_ready_jobs()
    assigned = job_queue.list_jobs()[0]
    assert assigned["assigned_printer_id"] == "p2"


def test_assign_ready_jobs_rejects_a_clearly_wrong_printer_model(tmp_path):
    state.printers["p1"] = FakePrinter("p1", printer_type="cc1")
    job_queue.add_job(_gcode(tmp_path, printer_model="centauri carbon 2"), "a.gcode", {})
    job_queue.assign_ready_jobs()
    assert job_queue.list_jobs()[0]["status"] == "queued"


def test_confirm_plate_cleared_uploads_and_starts_then_consumes_one_unit(tmp_path):
    printer = FakePrinter("p1")
    state.printers["p1"] = printer
    job_queue.add_job(_gcode(tmp_path), "a.gcode", {"quantity": 2})
    job_queue.assign_ready_jobs()
    run(job_queue.confirm_plate_cleared("p1"))
    assert printer.uploaded[2] is True  # start_after
    job = job_queue.list_jobs()[0]
    assert job["quantity"] == 1 and job["status"] == "queued" and job["assigned_printer_id"] is None


def test_confirm_plate_cleared_marks_done_when_quantity_reaches_zero(tmp_path):
    state.printers["p1"] = FakePrinter("p1")
    job_queue.add_job(_gcode(tmp_path), "a.gcode", {"quantity": 1})
    job_queue.assign_ready_jobs()
    run(job_queue.confirm_plate_cleared("p1"))
    assert job_queue.list_jobs()[0]["status"] == "done"


def test_confirm_plate_cleared_refuses_without_an_assigned_job(tmp_path):
    state.printers["p1"] = FakePrinter("p1")
    with pytest.raises(job_queue.QueueError):
        run(job_queue.confirm_plate_cleared("p1"))


def test_confirm_plate_cleared_refuses_unknown_printer():
    with pytest.raises(job_queue.QueueError):
        run(job_queue.confirm_plate_cleared("nope"))


def test_confirm_plate_cleared_leaves_job_assigned_when_start_fails(tmp_path):
    printer = FakePrinter("p1")
    printer.upload_result = False
    state.printers["p1"] = printer
    job_queue.add_job(_gcode(tmp_path), "a.gcode", {})
    job_queue.assign_ready_jobs()
    with pytest.raises(job_queue.QueueError):
        run(job_queue.confirm_plate_cleared("p1"))
    job = job_queue.list_jobs()[0]
    assert job["status"] == "assigned" and job["error"]
