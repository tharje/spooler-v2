"""Grab one JPEG frame from a printer's MJPEG camera stream."""

import urllib.parse

import camera_hub


def grab_jpeg(url: str, timeout: float = 5.0) -> bytes | None:
    """One recent JPEG from the camera, or None if it is unreachable, answers
    with an error, or doesn't produce a whole frame within `timeout` seconds.
    It is taken from the camera's shared connection (camera_hub.py) instead of
    opening and cutting off a stream of its own for every picture."""
    if not url:
        return None
    try:
        if urllib.parse.urlparse(url).scheme not in ("http", ""):
            return None
        return camera_hub.hubs.picture(url, timeout)
    except Exception:
        return None
