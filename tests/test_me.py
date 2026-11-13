import auth
import me


def test_device_names():
    assert me.device_name("Mozilla/5.0 (Linux; Android 14) AppleWebKit Chrome/120 Mobile Safari/537") == "Chrome on Android"
    assert me.device_name("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit Version/17 Mobile Safari/604") == "Safari on iPhone"
    assert me.device_name("Mozilla/5.0 (Windows NT 10.0) Gecko/20100101 Firefox/121.0") == "Firefox on Windows"
    assert me.device_name("Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537 Edg/120") == "Edge on Windows"
    assert me.device_name("") == "Unknown browser"


def _h(owner, g, s, state="complete", mat="PLA", printer="CC1", f="a.gcode"):
    return {"owner": owner, "filament_g": g, "print_time_s": s, "end_state": state, "completed": state == "complete",
            "material": mat, "printer_name": printer, "filename": f, "started_at": "2026-11-13T08:00:00"}


def test_print_stats_only_counts_own_prints():
    hist = [_h("kari", 600, 3600), _h("kari", 500, 13 * 3600, mat="PETG", f="long.gcode"),
            _h("kari", 10, 60, state="cancelled", printer="CC2"), _h("boss", 999, 99999), {"filament_g": 5}]
    s = me.print_stats(hist, "kari")
    assert s["prints"] == 3 and s["complete"] == 2 and s["success_rate"] == 67
    assert s["grams"] == 1110 and s["favourite_material"] == "PLA" and s["favourite_printer"] == "CC1"
    assert s["longest_file"] == "long.gcode" and s["longest_s"] == 13 * 3600
    got = {m["key"]: m["reached"] for m in s["milestones"]}
    assert got["first"] and got["kilo"] and got["long12"] and not got["long24"] and not got["ten"]


def test_no_owner_means_no_stats():
    assert me.print_stats([_h(None, 100, 100)], "")["prints"] == 0


def test_profile_keeps_to_own_data():
    subs = [{"endpoint": "https://fcm.googleapis.com/x", "owner": "kari", "device": "Chrome on Android", "added": 1},
            {"endpoint": "https://web.push.apple.com/y", "owner": "boss"}]
    jobs = [{"id": "1", "owner": "kari", "status": "queued", "filename": "a"},
            {"id": "2", "owner": "boss", "status": "queued", "filename": "b"},
            {"id": "3", "owner": "kari", "status": "done", "filename": "c"}]
    cmds = [{"user": "kari", "action": "home"}, {"user": "boss", "action": "home"}]
    p = me.profile("kari", role={"name": "operator", "label": "Operator"}, permission_labels={"view": "See", "x": "X"},
                   permissions={"view"}, printers=[], all_printers=True, jobs=jobs, commands=cmds, subs=subs,
                   history=[], events_own=False, events_on=3, channels_on=0, sessions=2)
    assert [j["id"] for j in p["jobs"]] == ["1"]
    assert p["commands"] == [{"user": "kari", "action": "home"}]
    assert [d["name"] for d in p["devices"]] == ["Chrome on Android"]
    assert p["devices"][0]["id"] == me.device_id("https://fcm.googleapis.com/x")
    assert p["permissions"] == [{"key": "view", "label": "See"}]


def test_sign_out_other_sessions():
    a = auth._create_session("kari")
    b = auth._create_session("kari")
    c = auth._create_session("boss")
    assert auth.user_session_count("kari") >= 2
    assert auth.sign_out_other_sessions("kari", a) >= 1
    assert auth._session_user(a) == "kari" and auth._session_user(b) == "" and auth._session_user(c) == "boss"
