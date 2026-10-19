"""One connection to each printer's camera, shared by every viewer and picture.

The CC2's camera service (a separate little program on the printer) serves at
most four streams at once and has been seen to stop answering until the printer
is power cycled. So Spooler keeps the number of connections, and the number of
times one is opened and cut off mid-stream, as low as it can:

* a single reader thread per camera URL pulls the MJPEG stream, cuts it into
  JPEG frames and hands the newest one to every browser viewer;
* single pictures (print end, notifications, spaghetti detection) are taken
  from that same stream. After a picture the connection stays open for LINGER
  seconds, so a detector asking once a minute reuses one connection for the
  whole print instead of opening and cutting off 60 per hour;
* when nobody has asked for a while the connection is closed, and it is
  re-opened (with back-off) if the stream stalls or drops;
* if the camera stops accepting connections altogether we slow down to one try
  a minute rather than hammering a dead port.
"""

import http.client
import threading
import time
import urllib.parse

_SOI, _EOI = b"\xff\xd8", b"\xff\xd9"
MAX_FRAME = 4 * 1024 * 1024
READ_TIMEOUT = 10.0          # no bytes for this long: the stream is stalled, reconnect
BACKOFF = (2, 5, 10, 30, 60)
LINGER = 90.0                # keep the stream open this long after a single picture
FRESH = 5.0                  # a frame this young counts as "now" for a single picture


class Subscriber:
    def __init__(self, hub):
        self._hub = hub
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0
        self._seen = 0

    def _push(self, frame: bytes) -> None:
        with self._cond:
            self._frame = frame
            self._seq += 1
            self._cond.notify_all()

    def next(self, timeout: float) -> bytes | None:
        """The next frame this subscriber has not seen, or None after `timeout`."""
        with self._cond:
            if self._seq == self._seen:
                self._cond.wait(timeout)
            if self._seq == self._seen:
                return None
            self._seen = self._seq
            return self._frame

    def close(self) -> None:
        self._hub._unsubscribe(self)


class _Hub:
    def __init__(self, url: str, registry):
        self.url = url
        self._registry = registry
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()
        self._thread = None
        self._linger_until = 0.0
        self._latest = None          # (monotonic time, jpeg)
        self._refused = False

    def _ensure_thread(self) -> None:
        # caller holds self._lock
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="camera-hub")
            self._thread.start()

    def _subscribe(self) -> Subscriber:
        sub = Subscriber(self)
        with self._lock:
            self._subs.append(sub)
            self._ensure_thread()
        return sub

    def _unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def _has_subs(self) -> bool:
        with self._lock:
            return bool(self._subs)

    def _wanted(self) -> bool:
        with self._lock:
            return bool(self._subs) or time.monotonic() < self._linger_until

    def _publish(self, frame: bytes) -> None:
        with self._lock:
            subs = list(self._subs)
            self._latest = (time.monotonic(), frame)
        for s in subs:
            s._push(frame)

    def _picture(self, timeout: float) -> bytes | None:
        """One recent frame, opening the stream if needed and keeping it open
        for a while afterwards."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self._linger_until = max(self._linger_until, time.monotonic() + LINGER)
            latest = self._latest
            if latest and time.monotonic() - latest[0] < FRESH:
                return latest[1]
            self._ensure_thread()
        # wait for a frame newer than the one we saw when we asked
        seen = latest[0] if latest else None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with self._lock:
                cur = self._latest
                if cur and cur[0] != seen:
                    return cur[1]
                if self._thread is None:    # reader gave up (camera refused or dropped)
                    return None
        return None

    def _run(self) -> None:
        failures = 0
        try:
            while self._wanted():
                got_frames = self._pull_once()
                failures = 0 if got_frames else failures + 1
                if not self._has_subs():
                    break      # only a single picture was wanted: don't keep retrying a dead camera
                time.sleep(BACKOFF[min(failures, len(BACKOFF) - 1)] if failures else 1)
        finally:
            with self._lock:
                self._thread = None
            self._registry._forget(self)

    def _pull_once(self) -> bool:
        """Read one upstream connection until it ends, stalls or nobody wants it.
        True if at least one frame arrived."""
        conn = None
        got = False
        try:
            parsed = urllib.parse.urlparse(self.url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=READ_TIMEOUT)
            conn.request("GET", path)
            resp = conn.getresponse()
            if resp.status != 200:
                print(f"[Camera] {parsed.hostname}: the printer's camera answered HTTP {resp.status}")
                return False
            buf = b""
            while self._wanted():
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(_SOI)
                    if start == -1:
                        buf = buf[-1:]
                        break
                    end = buf.find(_EOI, start + 2)
                    if end == -1:
                        buf = buf[start:]
                        if len(buf) > MAX_FRAME:
                            buf = b""
                        break
                    self._publish(buf[start:end + 2])
                    got = True
                    self._refused = False
                    buf = buf[end + 2:]
        except ConnectionRefusedError:
            if not self._refused:      # said once, not on every retry
                print(f"[Camera] {self.url}: the printer refuses camera connections "
                      "(its camera service has probably stopped; power cycling the printer brings it back)")
            self._refused = True
        except Exception as e:
            print(f"[Camera] could not read the printer's camera ({type(e).__name__}: {e})")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return got


class CameraHubs:
    def __init__(self):
        self._lock = threading.Lock()
        self._hubs: dict[str, _Hub] = {}

    def _hub(self, url: str) -> _Hub:
        with self._lock:
            hub = self._hubs.get(url)
            if hub is None:
                hub = self._hubs[url] = _Hub(url, self)
            return hub

    def subscribe(self, url: str) -> Subscriber:
        return self._hub(url)._subscribe()

    def picture(self, url: str, timeout: float = 5.0) -> bytes | None:
        """One recent JPEG from the camera, through the shared connection."""
        return self._hub(url)._picture(timeout)

    def _forget(self, hub: _Hub) -> None:
        with self._lock:
            if self._hubs.get(hub.url) is hub and not hub._subs and not hub._wanted():
                del self._hubs[hub.url]


hubs = CameraHubs()
