"""
File upload helpers: filename/extension validation, streaming request bodies
to disk (never the whole file in memory), housekeeping of the temp directory,
and a streaming multipart/form-data sender for protocols that need one.
"""

import hashlib
import http.client
import os
import re
import time
import uuid
from pathlib import Path

from persistence import DATA_DIR

UPLOAD_DIR = DATA_DIR / "uploads"
CHUNK = 256 * 1024
MAX_NAME_LEN = 100
ALLOWED_EXTENSIONS = {".gcode", ".gco", ".bgcode"}
STALE_AFTER_S = 24 * 3600


class UploadError(Exception):
    """Raised with a human-readable message that is safe to show in the UI."""


def max_upload_bytes() -> int:
    try:
        mb = float(os.getenv("SPOOLER_MAX_UPLOAD_MB", "500"))
    except ValueError:
        mb = 500.0
    return int(max(mb, 1) * 1024 * 1024)


def sanitize_filename(name: str) -> str:
    """Strip any path, control characters and shell-ish punctuation; keep
    letters (incl. æøå), digits, space and ._-()+ . Raises UploadError if
    nothing usable is left or the extension isn't allowed."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable())
    name = re.sub(r"[^\w .\-()+]", "_", name, flags=re.UNICODE).strip(" .")
    if not name:
        raise UploadError("Invalid file name.")
    stem, dot, ext = name.rpartition(".")
    if not dot or f".{ext.lower()}" not in ALLOWED_EXTENSIONS:
        raise UploadError(
            "Unsupported file type. Allowed: " + ", ".join(sorted(ALLOWED_EXTENSIONS)) + "."
        )
    if len(name) > MAX_NAME_LEN:
        keep = MAX_NAME_LEN - len(ext) - 1
        name = f"{stem[:keep]}.{ext}"
    return name


def save_stream(rfile, length: int, filename: str, max_bytes: int | None = None) -> Path:
    """Copy exactly `length` bytes from rfile into UPLOAD_DIR in chunks.
    Returns the saved path. The partial file is removed on any failure."""
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    if length <= 0:
        raise UploadError("Empty upload.")
    if length > limit:
        raise UploadError(f"File too large (limit {limit // (1024 * 1024)} MB).")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}-{filename}"
    remaining = length
    try:
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk = rfile.read(min(CHUNK, remaining))
                if not chunk:
                    raise UploadError("Upload interrupted.")
                f.write(chunk)
                remaining -= len(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


def remove_stale_uploads(max_age_s: int = STALE_AFTER_S) -> int:
    if not UPLOAD_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for p in UPLOAD_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def forward_timeout(size: int) -> float:
    """Seconds allowed for the printer-side transfer: 60 s base plus 1 s per
    MB (assumes >= 1 MB/s, conservative for Wi-Fi printers)."""
    return 60.0 + size / (1024 * 1024)


def on_file_uploaded(printer_id: str, local_path: Path, filename: str) -> None:
    """Hook called after the file is safely on disk and before it is sent to
    the printer. G-code analysis (roadmap appendix) plugs in here later."""
    return None


def post_multipart_file(host: str, port: int, path: str, fields: dict, file_field: str,
                        filename: str, local_path: Path, headers: dict | None = None,
                        timeout: float = 60.0) -> tuple[int, bytes]:
    """POST multipart/form-data, streaming the file from disk. `fields` are
    sent before the file part. Returns (status, response body)."""
    boundary = uuid.uuid4().hex
    head = b""
    for k, v in fields.items():
        head += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    safe_name = filename.replace('"', "_")
    head += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
             f"filename=\"{safe_name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = local_path.stat().st_size
    hdrs = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(head) + size + len(tail)),
        **(headers or {}),
    }
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.putrequest("POST", path)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(head)
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                conn.send(chunk)
        conn.send(tail)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def put_file(host: str, port: int, path: str, local_path: Path, headers: dict | None = None,
             timeout: float = 60.0) -> tuple[int, bytes]:
    """PUT the raw file body, streamed from disk."""
    size = local_path.stat().st_size
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.putrequest("PUT", path)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                conn.send(chunk)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()
