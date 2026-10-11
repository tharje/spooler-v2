"""
Spaghetti detection through Obico's open ML API (the "Spaghetti Detective"
detector, run by you as its own container).

While a print is running, Spooler takes a camera picture every so often and
asks the detector what it sees. Obico's ML API fetches the picture itself from
a web address, so Spooler hands out each picture on a one-time address that
works for a few seconds. When several pictures in a row look like a failed
print, one notification with the picture is sent for that print. Nothing is
ever done to the printer.
"""

import asyncio
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

import config
import state
from features import is_enabled

PRINTING = {2, 3, 4, 13}              # printers.base.PRINTING_STATUSES
IMAGE_TTL_S = 30
_images: dict = {}                    # one-time token -> (jpeg, expires monotonic)
_lock = threading.Lock()

# A 1x1 picture, used by "Test connection" when no printer camera is available.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f14"
    "1d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b08000100010101"
    "1100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504"
    "040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a2526"
    "2728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a"
    "92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6"
    "e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfcffd9")


class DetectError(Exception):
    pass


def settings() -> dict:
    return {
        "url": (config.get("spaghetti.url") or "").rstrip("/"),
        "token": config.get("spaghetti.token") or "",
        "spooler_url": (config.get("spaghetti.spooler_url") or "").rstrip("/"),
        "threshold": min(95, max(10, int(config.get("spaghetti.threshold") or 50))),
        "frames": min(10, max(1, int(config.get("spaghetti.frames") or 3))),
        "interval_s": min(600, max(15, int(config.get("spaghetti.interval_s") or 60))),
    }


def configured(s: dict | None = None) -> bool:
    s = s or settings()
    return bool(s["url"] and s["spooler_url"])


# ── one-time picture addresses ───────────────────────────────────────────────

def offer_image(jpeg: bytes) -> str:
    token = secrets.token_urlsafe(18)
    now = time.monotonic()
    with _lock:
        for t in [t for t, (_, exp) in _images.items() if exp < now]:
            del _images[t]
        _images[token] = (jpeg, now + IMAGE_TTL_S)
    return token


def take_image(token: str) -> bytes | None:
    """The picture for this address, once; None when unknown, used or expired."""
    with _lock:
        item = _images.pop(token, None)
    return item[0] if item and item[1] >= time.monotonic() else None


# ── the detector ─────────────────────────────────────────────────────────────

def best_confidence(detections) -> float:
    """Highest confidence among the detections (0 when there are none)."""
    best = 0.0
    for d in detections or []:
        try:
            best = max(best, float(d[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return best


def ask_detector(jpeg: bytes, s: dict | None = None, timeout: float = 15.0) -> list:
    """Give the detector one picture; returns its list of detections."""
    s = s or settings()
    if not configured(s):
        raise DetectError("Set the detector address and Spooler's address first.")
    token = offer_image(jpeg)
    try:
        img = f"{s['spooler_url']}/api/spaghetti-image/{token}"
        req = urllib.request.Request(f"{s['url']}/p/?img={urllib.parse.quote(img, safe='')}")
        if s["token"]:
            req.add_header("Authorization", f"Bearer {s['token']}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(1_000_000))
        return data["detections"]
    except urllib.error.HTTPError as e:
        raise DetectError("The detector refused the access token." if e.code == 401
                          else f"The detector answered HTTP {e.code}.")
    except (KeyError, TypeError, ValueError):
        raise DetectError("The detector's answer was not understood.")
    except Exception as e:
        raise DetectError(f"Could not reach the detector ({type(e).__name__}).")
    finally:
        take_image(token)


def test_connection(jpeg: bytes | None = None) -> dict:
    try:
        found = ask_detector(jpeg or TINY_JPEG)
    except DetectError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "message": f"The detector answered ({len(found)} detections in the test picture)."}


# ── watching the printers ────────────────────────────────────────────────────

class Watch:
    """Per printer: how many pictures in a row looked bad, and whether this print was already announced."""
    def __init__(self):
        self.bad = 0
        self.announced = False

    def reset(self):
        self.bad = 0
        self.announced = False

    def feed(self, confidence: float, s: dict) -> bool:
        """Count one picture; True exactly once per print, when it has looked bad often enough."""
        if confidence * 100 >= s["threshold"]:
            self.bad += 1
        else:
            self.bad = 0
        if self.bad >= s["frames"] and not self.announced:
            self.announced = True
            return True
        return False


_watches: dict = {}


async def check_printer(p, s: dict) -> None:
    watch = _watches.setdefault(p.id, Watch())
    status = p._decoded_printinfo().get("Status")
    if status not in PRINTING:
        watch.reset()
        return
    jpeg = await p._camera_picture(max_age_s=0)
    if not jpeg:
        return
    try:
        found = await asyncio.get_running_loop().run_in_executor(None, ask_detector, jpeg, s)
    except DetectError as e:
        print(f"[Spaghetti] {p.name}: {e}")
        return
    if watch.feed(best_confidence(found), s):
        p._emit("spaghetti", f"{p.name} — Possible print failure",
                "The detector thinks this print may be failing (spaghetti). Check the printer.",
                priority="urgent")


async def watch_loop() -> None:
    while True:
        s = settings()
        await asyncio.sleep(s["interval_s"])
        if not (is_enabled("spaghetti_detection") and is_enabled("camera") and configured()):
            continue
        for p in list(state.printers.values()):
            try:
                await check_printer(p, s)
            except Exception as e:
                print(f"[Spaghetti] {p.name}: {type(e).__name__}")
