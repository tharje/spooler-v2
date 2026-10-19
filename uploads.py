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


CC2_UPLOAD_CHUNK = 1024 * 1024  # Elegoo's SDK sends at most 1 MB per request


def put_file_chunked(host: str, port: int, path: str, local_path: Path, remote_name: str,
                     token: str, chunk_size: int = CC2_UPLOAD_CHUNK,
                     timeout: float = 60.0) -> dict:
    """Upload the way Elegoo's elegoo-link SDK does for the Centauri Carbon 2:
    repeated PUTs of <= 1 MB with a Content-Range, X-File-Name, X-File-MD5 and
    X-Token header, over one kept-alive connection. Each reply is JSON like
    {"error_code": 0, "offset": N}. Returns the last reply; raises UploadError
    if the printer reports a non-zero error_code or a non-200 status."""
    import json
    size = local_path.stat().st_size
    if size <= 0:
        raise UploadError("Empty upload.")
    md5 = file_md5(local_path)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    last: dict = {}
    try:
        with open(local_path, "rb") as f:
            offset = 0
            while offset < size:
                data = f.read(min(chunk_size, size - offset))
                if not data:
                    raise UploadError("Upload interrupted (file changed while sending).")
                end = offset + len(data) - 1
                conn.request("PUT", path, body=data, headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                    "X-File-Name": remote_name,
                    "X-File-MD5": md5,
                    "X-Token": token,
                    "User-Agent": "ElegooLink/1.3.6",
                })
                resp = conn.getresponse()
                body = resp.read()
                text = body.decode("utf-8", errors="replace")[:300]
                if resp.status != 200:
                    raise UploadError(f"Printer rejected the upload (HTTP {resp.status}): {text}")
                try:
                    last = json.loads(body)
                except ValueError:
                    raise UploadError(f"Unexpected reply from the printer: {text}")
                code = last.get("error_code", 0)
                if code not in (0, None):
                    raise UploadError(f"Printer rejected the upload (error {code}).")
                offset += len(data)
        return last
    finally:
        conn.close()
