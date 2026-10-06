"""Grab one JPEG frame from a printer's MJPEG camera stream."""

import http.client
import time
import urllib.parse

_SOI, _EOI = b"\xff\xd8", b"\xff\xd9"
MAX_BYTES = 4 * 1024 * 1024


def grab_jpeg(url: str, timeout: float = 5.0) -> bytes | None:
    """Read the stream until the first complete JPEG (FFD8 ... FFD9) and return
    it, or None if the camera is unreachable, answers with an error, or
    doesn't produce a whole frame within `timeout` seconds in total."""
    if not url:
        return None
    deadline = time.monotonic() + timeout
    conn = None
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", ""):
            return None
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        buf = b""
        while time.monotonic() < deadline and len(buf) < MAX_BYTES:
            chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
            if not chunk:
                break
            buf += chunk
            start = buf.find(_SOI)
            if start == -1:
                buf = buf[-1:]  # keep a possible split FF
                continue
            end = buf.find(_EOI, start + 2)
            if end != -1:
                return buf[start:end + 2]
        return None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
