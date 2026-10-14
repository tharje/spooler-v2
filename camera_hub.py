"""One connection to each printer's camera, shared by every viewer.

The printers' camera services are small and handle only a few simultaneous
streams. Opening one upstream connection per browser tab (and per refresh or
return from the background) could leave the printer's camera daemon stuck until
the printer was power cycled. Here a single reader thread per camera URL pulls
the MJPEG stream, cuts it into JPEG frames and hands the newest frame to every
subscriber. The upstream connection is closed as soon as the last viewer leaves,
and re-opened (with back-off) if the stream stalls or drops.
"""

import http.client
import threading
import time
import urllib.parse

_SOI, _EOI = b"\xff\xd8", b"\xff\xd9"
MAX_FRAME = 4 * 1024 * 1024
READ_TIMEOUT = 10.0          # no bytes for this long: the stream is stalled, reconnect
BACKOFF = (2, 5, 10, 30)


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

    def _subscribe(self) -> Subscriber:
        sub = Subscriber(self)
        with self._lock:
            self._subs.append(sub)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="camera-hub")
                self._thread.start()
        return sub

    def _unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def _has_subs(self) -> bool:
        with self._lock:
            return bool(self._subs)

    def _publish(self, frame: bytes) -> None:
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s._push(frame)

    def _run(self) -> None:
        failures = 0
        try:
            while self._has_subs():
                got_frames = self._pull_once()
                failures = 0 if got_frames else failures + 1
                if not self._has_subs():
                    break
                time.sleep(BACKOFF[min(failures, len(BACKOFF) - 1)] if failures else 1)
        finally:
            with self._lock:
                self._thread = None
            self._registry._forget(self)

    def _pull_once(self) -> bool:
        """Read one upstream connection until it ends, stalls or nobody is watching.
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
            while self._has_subs():
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
                    buf = buf[end + 2:]
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

    def subscribe(self, url: str) -> Subscriber:
        with self._lock:
            hub = self._hubs.get(url)
            if hub is None:
                hub = self._hubs[url] = _Hub(url, self)
            return hub._subscribe()

    def _forget(self, hub: _Hub) -> None:
        with self._lock:
            if self._hubs.get(hub.url) is hub and not hub._subs:
                del self._hubs[hub.url]


hubs = CameraHubs()
