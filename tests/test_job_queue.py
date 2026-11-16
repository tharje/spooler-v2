"""T20: the print queue -- add/list/update/delete, assignment to a matching
idle printer, and confirm_plate_cleared's upload+start."""

import asyncio
import threading

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
        self.started = None
        self.upload_result = True
        self.start_result = True

    def to_dict(self):
        return {"state": self._state}

    def _tray_spool(self):
        return self._tray

    async def upload_file(self, local_path, remote_name, start_after=False):
        self.uploaded = (local_path, remote_name, start_after)
        return self.upload_result

    async def start_print_file(self, filename, print_opts=None):
        self.started = filename
        self.started_opts = print_opts
        return self.start_result


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


def test_add_existing_job_ties_to_that_printer_and_needs_no_file():
    state.printers["p1"] = FakePrinter("p1", printer_type="cc2")
    job = job_queue.add_existing_job("p1", "/local/benchy.gcode", {"quantity": 2})
    assert job["source"] == "printer_file"
    assert job["printer_id"] == "p1" and job["printer_type"] == "cc2"
    assert job["filename"] == "/local/benchy.gcode"
    assert not (job_queue.QUEUE_DIR / f"{job['id']}.gcode").exists()


def test_add_existing_job_refuses_unknown_printer():
    with pytest.raises(job_queue.QueueError):
        job_queue.add_existing_job("nope", "/local/a.gcode", {})


def test_confirm_plate_cleared_starts_an_existing_printer_file_without_uploading():
    printer = FakePrinter("p1")
    state.printers["p1"] = printer
    job_queue.add_existing_job("p1", "/local/benchy.gcode", {"quantity": 1})
    job_queue.assign_ready_jobs()
    run(job_queue.confirm_plate_cleared("p1"))
    assert printer.uploaded is None  # never uploaded, only started
    assert printer.started == "/local/benchy.gcode"
    assert job_queue.list_jobs()[0]["status"] == "done"


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


def test_queued_file_keeps_its_print_info_for_the_start_dialog(tmp_path):
    path = tmp_path / "two.gcode"
    path.write_bytes(b"; estimated printing time (normal mode) = 1h 8m 44s\n; filament used [g] = 36.21\n"
                     b"; filament_colour = #F2754E;#000000\n; filament_type = PETG;PLA\n" + b"x" * 100)
    job_queue.add_job(path, "two.gcode", {})
    info = job_queue.list_jobs()[0]["print_info"]
    assert info == {"print_time": 4124, "filament_g": 36.21, "color_map": [
        {"t": 0, "color": "#F2754E", "name": "PETG"}, {"t": 1, "color": "#000000", "name": "PLA"}]}


def test_jobs_queued_before_print_info_get_it_when_listed(tmp_path):
    job_queue.add_job(_gcode(tmp_path), "a.gcode", {})
    jobs = job_queue._load()
    del jobs[0]["print_info"]
    job_queue._save(jobs)
    assert job_queue.list_jobs()[0]["print_info"] == {"print_time": None, "filament_g": None, "color_map": []}
    assert "print_info" in job_queue._load()[0]


def test_clean_print_opts_keeps_only_the_start_dialog_choices():
    assert job_queue.clean_print_opts(None) is None
    assert job_queue.clean_print_opts({"leveling": 1, "slot_map": [{"t": 0, "canvas_id": 0, "tray_id": 2}], "rm": "-rf"}) == {
        "leveling": True, "timelapse": False, "smooth_plate": False, "slot_map": [{"t": 0, "canvas_id": 0, "tray_id": 2}]}
    assert "slot_map" not in job_queue.clean_print_opts({"slot_map": "all"})


def test_confirm_plate_cleared_starts_with_the_dialog_choices(tmp_path):
    printer = FakePrinter("p1")
    state.printers["p1"] = printer
    job_queue.add_job(_gcode(tmp_path), "a.gcode", {})
    job_queue.assign_ready_jobs()
    opts = {"leveling": True, "timelapse": False, "smooth_plate": False, "slot_map": [{"t": 0, "canvas_id": 0, "tray_id": 1}]}
    run(job_queue.confirm_plate_cleared("p1", opts))
    assert printer.uploaded[2] is False   # uploaded, then started with the options
    assert printer.started == "a.gcode" and printer.started_opts == opts
    assert job_queue.list_jobs()[0]["status"] == "done"


def test_confirm_route_passes_the_dialog_choices(monkeypatch):
    import http_admin
    import http_handler
    h = http_handler.SPHandler.__new__(http_handler.SPHandler)
    h.path = "/api/queue/p1/confirm"
    h._can_see_printer = lambda pid: True
    h._read_body = lambda: b'{"print_opts": {"leveling": true}}'
    got = []
    h._json = lambda body, code=200: got.append((code, body))

    async def confirm(printer_id, print_opts=None):
        return {"printer": printer_id, "opts": print_opts}
    monkeypatch.setattr(job_queue, "confirm_plate_cleared", confirm)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    monkeypatch.setattr(http_admin.http_common, "ws_loop", lambda: loop)
    h._handle_queue_confirm()
    loop.call_soon_threadsafe(loop.stop)
    assert got == [(200, {"ok": True, "job": {"printer": "p1", "opts": {"leveling": True, "timelapse": False, "smooth_plate": False}}})]
