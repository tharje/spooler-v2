"""Service logbook: entries, print-hour reminders announced once, backup."""

import json

import pytest

import backup
import persistence
import service


def hist(*hours, printer="p1"):
    persistence.HISTORY_FILE.write_text(json.dumps(
        [{"id": f"h{i}", "printer_id": printer, "print_time_s": int(h * 3600)} for i, h in enumerate(hours)]))


def test_print_hours_come_from_the_history():
    hist(2, 1.5)
    assert service.print_hours("p1") == 3.5
    assert service.print_hours("other") == 0


def test_logbook_entry_records_hours_and_is_newest_first():
    hist(10)
    service.add_log("p1", "lubrication", "Z rods", date="2026-01-01")
    service.add_log("p1", "nozzle", "", date="2026-03-01")
    log = service.overview({"p1": "CC1"})["log"]
    assert [e["kind"] for e in log] == ["nozzle", "lubrication"]
    assert log[0]["hours"] == 10


def test_bad_input_is_refused():
    for args in [("", "nozzle"), ("p1", "magic")]:
        with pytest.raises(service.ServiceError):
            service.add_log(*args)
    with pytest.raises(service.ServiceError):
        service.add_log("p1", "nozzle", date="yesterday")
    for name, hrs in [("", 10), ("x", 0), ("x", "abc"), ("x", 10**7)]:
        with pytest.raises(service.ServiceError):
            service.add_task("p1", name, hrs)


def test_reminder_is_announced_once_and_again_after_it_is_done():
    hist(5)
    t = service.add_task("p1", "Lubricate Z", 20)
    assert service.take_due("p1") == []
    hist(5, 21)                                   # 21 more hours since the reminder was set
    due = service.take_due("p1")
    assert [d["name"] for d in due] == ["Lubricate Z"] and due[0]["hours_since"] == 21
    assert service.take_due("p1") == []           # not repeated while still not done
    service.add_log("p1", "lubrication", task_id=t["id"])
    view = service.overview({"p1": "CC1"})["tasks"][0]
    assert view["due"] is False and view["hours_since"] == 0
    hist(5, 21, 25)
    assert len(service.take_due("p1")) == 1       # due again after the next interval


def test_reminder_only_for_its_own_printer():
    hist(1, printer="p2")
    service.add_task("p2", "Belt", 1)
    hist(1, 5, printer="p2")
    assert service.take_due("p1") == []
    assert len(service.take_due("p2")) == 1


def test_delete_task_and_log():
    t = service.add_task("p1", "x", 5)
    e = service.add_log("p1", "other", "y")
    assert service.delete_task(t["id"]) and not service.delete_task(t["id"])
    assert service.delete_log(e["id"]) and not service.delete_log(e["id"])


def test_logbook_is_part_of_backups():
    assert "service_log.json" in backup.BACKUP_FILES


def test_hours_keep_counting_when_the_history_is_trimmed():
    hist(10)
    service.add_print_time("p1", 3600)                 # first print seen: counter starts from the history (10 h)
    assert service.print_hours("p1") == 10
    hist()                                             # history trimmed away
    service.add_print_time("p1", 7200)
    assert service.print_hours("p1") == 12


def test_reminder_counted_in_days(monkeypatch):
    t = service.add_task("p1", "Check belts", 30, unit="days")
    assert service.overview({"p1": "CC1"})["tasks"][0]["unit"] == "days"
    assert service.take_due("p1") == []
    real = service.time.time
    monkeypatch.setattr(service.time, "time", lambda: real() + 31 * 86400)
    due = service.take_due("p1")
    assert len(due) == 1 and due[0]["since"] == 31 and due[0]["unit"] == "days"
    service.add_log("p1", "belt", task_id=t["id"])
    assert service.overview({"p1": "CC1"})["tasks"][0]["due"] is False


def test_bad_unit_is_refused():
    with pytest.raises(service.ServiceError):
        service.add_task("p1", "x", 5, unit="weeks")


def test_total_print_hours_can_be_set_and_reminders_keep_their_countdown():
    hist(10)
    t = service.add_task("p1", "Lubricate", 100)
    service.add_print_time("p1", 3600)                       # counter starts at 10 h
    hist(10)
    service.add_print_time("p1", 5 * 3600)                   # 15 h: 5 since the reminder
    service.set_print_hours("p1", 250)                       # the printer had a long life before Spooler
    assert service.print_hours("p1") == 250
    view = service.overview({"p1": "CC1"})["tasks"][0]
    assert view["hours_since"] == 5 and not view["due"]
    service.add_print_time("p1", 3600)
    assert service.print_hours("p1") == 251


def test_bad_total_is_refused():
    for bad in ("abc", -1, 10**9):
        with pytest.raises(service.ServiceError):
            service.set_print_hours("p1", bad)
