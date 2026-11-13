"""
The signed-in user's own page ("Me"): who they are, what they may do, their
devices, queue jobs and commands, and statistics for the prints they started.

The functions here are pure (they get the data passed in) except the thin
gathering in profile(), so they are easy to test.
"""

import hashlib
import time
import urllib.parse

# (key, title, test(stats) -> (reached, progress 0-1))
MILESTONES = (
    ("first",   "First print",            lambda s: (s["prints"] >= 1, min(1, s["prints"]))),
    ("ten",     "10 prints",              lambda s: (s["prints"] >= 10, s["prints"] / 10)),
    ("fifty",   "50 prints",              lambda s: (s["prints"] >= 50, s["prints"] / 50)),
    ("hundred", "100 prints",             lambda s: (s["prints"] >= 100, s["prints"] / 100)),
    ("kilo",    "First kilo of filament", lambda s: (s["grams"] >= 1000, s["grams"] / 1000)),
    ("fivekg",  "5 kg of filament",       lambda s: (s["grams"] >= 5000, s["grams"] / 5000)),
    ("long12",  "A print over 12 hours",  lambda s: (s["longest_s"] >= 12 * 3600, s["longest_s"] / (12 * 3600))),
    ("long24",  "A print over 24 hours",  lambda s: (s["longest_s"] >= 24 * 3600, s["longest_s"] / (24 * 3600))),
    ("hours100", "100 hours of printing", lambda s: (s["hours"] >= 100, s["hours"] / 100)),
)


def device_name(user_agent: str) -> str:
    """A short, readable name for a browser: "Chrome on Android", "Safari on iPhone"."""
    ua = user_agent or ""
    if "iPhone" in ua:
        os_ = "iPhone"
    elif "iPad" in ua:
        os_ = "iPad"
    elif "Android" in ua:
        os_ = "Android"
    elif "Windows" in ua:
        os_ = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_ = "Mac"
    elif "Linux" in ua:
        os_ = "Linux"
    else:
        os_ = ""
    if "Edg/" in ua:
        br = "Edge"
    elif "Firefox/" in ua or "FxiOS" in ua:
        br = "Firefox"
    elif "SamsungBrowser" in ua:
        br = "Samsung Internet"
    elif "Chrome/" in ua or "CriOS" in ua:
        br = "Chrome"
    elif "Safari/" in ua:
        br = "Safari"
    else:
        br = ""
    if br and os_:
        return f"{br} on {os_}"
    return br or os_ or "Unknown browser"


def device_id(endpoint: str) -> str:
    """Stable, non-secret id for a push subscription (the endpoint itself is a secret-ish URL)."""
    return hashlib.sha256((endpoint or "").encode()).hexdigest()[:16]


def _push_service(endpoint: str) -> str:
    host = urllib.parse.urlparse(endpoint or "").hostname or ""
    if host.endswith("googleapis.com"):
        return "Chrome / Android"
    if "mozilla" in host:
        return "Firefox"
    if host.endswith("apple.com"):
        return "Safari / iPhone / iPad"
    if "windows.com" in host:
        return "Edge / Windows"
    return host


def devices_for(subs: list, owner: str) -> list:
    return [{"id": device_id(s.get("endpoint", "")),
             "name": s.get("device") or _push_service(s.get("endpoint", "")),
             "added": s.get("added")}
            for s in subs if s.get("owner", "") == owner]


def print_stats(history: list, owner: str) -> dict:
    """Statistics for the prints `owner` started from Spooler (history "owner")."""
    mine = [e for e in history if owner and e.get("owner") == owner]
    complete = sum(1 for e in mine if e.get("end_state") == "complete" or e.get("completed"))
    grams = sum(float(e.get("filament_g") or 0) for e in mine)
    secs = sum(int(e.get("print_time_s") or 0) for e in mine)
    materials, printers = {}, {}
    for e in mine:
        if e.get("material"):
            materials[e["material"]] = materials.get(e["material"], 0) + float(e.get("filament_g") or 0)
        name = e.get("printer_name") or e.get("printer_id") or ""
        if name:
            printers[name] = printers.get(name, 0) + 1
    longest = max(mine, key=lambda e: int(e.get("print_time_s") or 0), default=None)
    biggest = max(mine, key=lambda e: float(e.get("filament_g") or 0), default=None)
    stats = {
        "prints": len(mine),
        "complete": complete,
        "success_rate": round(100 * complete / len(mine)) if mine else None,
        "grams": round(grams, 1),
        "hours": round(secs / 3600, 1),
        "favourite_material": max(materials, key=materials.get) if materials else None,
        "favourite_printer": max(printers, key=printers.get) if printers else None,
        "favourite_printer_prints": max(printers.values()) if printers else 0,
        "longest_s": int(longest.get("print_time_s") or 0) if longest else 0,
        "longest_file": (longest.get("filename") or "") if longest else "",
        "biggest_g": float(biggest.get("filament_g") or 0) if biggest else 0,
        "biggest_file": (biggest.get("filename") or "") if biggest else "",
        "first_at": min((e.get("started_at") or e.get("timestamp") or "" for e in mine), default=None) or None,
    }
    stats["milestones"] = []
    for key, title, test in MILESTONES:
        reached, progress = test(stats)
        stats["milestones"].append({"key": key, "title": title, "reached": bool(reached),
                                    "progress": round(max(0.0, min(1.0, float(progress))), 3)})
    return stats


def profile(user: str, *, role: dict | None, permission_labels: dict, permissions: set,
            printers: list, all_printers: bool, jobs: list, commands: list, subs: list,
            history: list, events_own: bool, events_on: int, channels_on: int, sessions: int) -> dict:
    return {
        "user": user,
        "role": {"name": (role or {}).get("name", ""), "label": (role or {}).get("label", "")},
        "permissions": [{"key": k, "label": permission_labels.get(k, k)}
                        for k in permission_labels if k in permissions],
        "printers": printers,
        "all_printers": all_printers,
        "jobs": [{"id": j.get("id"), "filename": j.get("filename"), "status": j.get("status"),
                  "printer": j.get("printer_name") or j.get("printer_id") or "", "added_at": j.get("added_at")}
                 for j in jobs if j.get("owner") == user and j.get("status") not in ("done", "cancelled", "failed")],
        "commands": [c for c in commands if c.get("user") == user][:15],
        "devices": devices_for(subs, user),
        "notifications": {"own_events": events_own, "events_on": events_on, "channels_on": channels_on},
        "sessions": sessions,
        "stats": print_stats(history, user),
        "generated": int(time.time()),
    }
