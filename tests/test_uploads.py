import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import features
import uploads


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path / "uploads")


def test_sanitize_strips_paths_and_keeps_nordic():
    assert uploads.sanitize_filename("../../etc/Bjørn åpen.GCODE") == "Bjørn åpen.GCODE"
    assert uploads.sanitize_filename("C:\\x\\a.gcode") == "a.gcode"


@pytest.mark.parametrize("bad", ["", "..", "a.exe", "noext", "a.gcode.txt", "   .gcode"])
def test_sanitize_rejects(bad):
    with pytest.raises(uploads.UploadError):
        uploads.sanitize_filename(bad)


def test_sanitize_limits_length_keeps_extension():
    out = uploads.sanitize_filename("a" * 300 + ".gcode")
    assert len(out) <= uploads.MAX_NAME_LEN and out.endswith(".gcode")


def test_save_stream_writes_exact_length_in_chunks(monkeypatch):
    monkeypatch.setattr(uploads, "CHUNK", 7)
    data = b"G1 X1\n" * 50
    stream = io.BytesIO(data + b"EXTRA")
    p = uploads.save_stream(stream, len(data), "a.gcode", max_bytes=10_000)
    assert p.read_bytes() == data
    assert stream.read() == b"EXTRA"  # never reads past Content-Length


def test_save_stream_rejects_too_large_and_empty():
    with pytest.raises(uploads.UploadError, match="too large"):
        uploads.save_stream(io.BytesIO(b"x" * 10), 10, "a.gcode", max_bytes=5)
    with pytest.raises(uploads.UploadError):
        uploads.save_stream(io.BytesIO(b""), 0, "a.gcode")


def test_save_stream_cleans_up_interrupted_upload():
    with pytest.raises(uploads.UploadError, match="interrupted"):
        uploads.save_stream(io.BytesIO(b"abc"), 10, "a.gcode", max_bytes=100)
    assert list(uploads.UPLOAD_DIR.iterdir()) == []


def test_max_upload_env(monkeypatch):
    monkeypatch.setenv("SPOOLER_MAX_UPLOAD_MB", "2")
    assert uploads.max_upload_bytes() == 2 * 1024 * 1024
    monkeypatch.setenv("SPOOLER_MAX_UPLOAD_MB", "junk")
    assert uploads.max_upload_bytes() == 500 * 1024 * 1024


def test_remove_stale_uploads(monkeypatch):
    import os, time
    uploads.UPLOAD_DIR.mkdir(parents=True)
    old, new = uploads.UPLOAD_DIR / "old.gcode", uploads.UPLOAD_DIR / "new.gcode"
    old.write_bytes(b"1"); new.write_bytes(b"1")
    t = time.time() - 48 * 3600
    os.utime(old, (t, t))
    assert uploads.remove_stale_uploads() == 1
    assert not old.exists() and new.exists()


def test_forward_timeout_scales_with_size():
    assert uploads.forward_timeout(0) == 60
    assert uploads.forward_timeout(100 * 1024 * 1024) == 160


class _Capture(BaseHTTPRequestHandler):
    seen = {}

    def _record(self):
        n = int(self.headers.get("Content-Length", 0))
        _Capture.seen = {"method": self.command, "path": self.path,
                         "headers": dict(self.headers), "body": self.rfile.read(n)}
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
        self.wfile.write(b"ok")

    do_POST = do_PUT = _record

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_post_multipart_streams_fields_then_file(tmp_path, server):
    f = tmp_path / "a.gcode"; f.write_bytes(b"G28\n")
    status, body = uploads.post_multipart_file(
        "127.0.0.1", server, "/up", {"root": "gcodes", "print": "true"}, "file", "a.gcode", f)
    seen = _Capture.seen
    assert (status, body) == (200, b"ok")
    assert seen["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert int(seen["headers"]["Content-Length"]) == len(seen["body"])
    b = seen["body"]
    assert b.index(b'name="root"') < b.index(b'name="print"') < b.index(b'name="file"; filename="a.gcode"')
    assert b"G28\n" in b


def test_put_file_sends_raw_body_and_headers(tmp_path, server):
    f = tmp_path / "a.gcode"; f.write_bytes(b"G28\n")
    uploads.put_file("127.0.0.1", server, "/api/v1/files/usb/a.gcode", f, {"Print-After-Upload": "?1"})
    seen = _Capture.seen
    assert seen["method"] == "PUT" and seen["body"] == b"G28\n"
    assert seen["headers"]["Print-After-Upload"] == "?1"


def test_file_upload_feature_registered_and_gates_handler():
    assert features.FEATURES["file_upload"].default is True
    import http_handler
    assert http_handler.SPHandler._handle_upload._feature_gate == "file_upload"


def test_supports_upload_flags():
    from printers.cc1 import CC1Connection
    from printers.cc2 import CC2Connection
    from printers.moonraker import MoonrakerConnection
    from printers.prusa import PrusaConnection
    assert CC1Connection.supports_upload and MoonrakerConnection.supports_upload
    assert PrusaConnection.supports_upload
    assert CC2Connection.supports_upload  # follows Elegoo's SDK; unverified on hardware
    from printers.base import PrinterConnection
    assert not PrinterConnection.supports_upload
    assert CC2Connection("id", "1.2.3.4", "n").to_dict()["supports_upload"] is True


def test_unsupported_printer_upload_raises_readable_error():
    import asyncio
    from printers.base import PrinterConnection
    with pytest.raises(uploads.UploadError, match="not supported"):
        asyncio.run(PrinterConnection("id", "1.2.3.4", "n").upload_file(None, "a.gcode"))


class _CC2Printer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []
    reply = b'{"error_code": 0, "offset": 0}'
    status = 200

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        _CC2Printer.requests.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)})
        self.send_response(_CC2Printer.status)
        self.send_header("Content-Length", str(len(_CC2Printer.reply)))
        self.end_headers()
        self.wfile.write(_CC2Printer.reply)

    def log_message(self, *a):
        pass


@pytest.fixture
def cc2_server():
    _CC2Printer.requests = []
    _CC2Printer.reply, _CC2Printer.status = b'{"error_code": 0, "offset": 0}', 200
    srv = HTTPServer(("127.0.0.1", 0), _CC2Printer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_put_file_chunked_splits_with_content_range_and_headers(tmp_path, cc2_server):
    data = bytes(range(256)) * 10  # 2560 bytes
    f = tmp_path / "a.gcode"; f.write_bytes(data)
    uploads.put_file_chunked("127.0.0.1", cc2_server, "/upload", f, "My file.gcode", "1234", chunk_size=1000)
    reqs = _CC2Printer.requests
    assert [r["headers"]["Content-Range"] for r in reqs] == [
        "bytes 0-999/2560", "bytes 1000-1999/2560", "bytes 2000-2559/2560"]
    assert b"".join(r["body"] for r in reqs) == data
    h = reqs[0]["headers"]
    assert h["X-File-Name"] == "My file.gcode" and h["X-Token"] == "1234"
    assert h["X-File-MD5"] == uploads.file_md5(f)
    assert reqs[0]["path"] == "/upload"


def test_put_file_chunked_raises_on_printer_error_code(tmp_path, cc2_server):
    _CC2Printer.reply = b'{"error_code": 9004}'
    f = tmp_path / "a.gcode"; f.write_bytes(b"G28\n")
    with pytest.raises(uploads.UploadError, match="9004"):
        uploads.put_file_chunked("127.0.0.1", cc2_server, "/upload", f, "a.gcode", "123456")


def test_put_file_chunked_raises_on_http_error(tmp_path, cc2_server):
    _CC2Printer.status = 500
    f = tmp_path / "a.gcode"; f.write_bytes(b"G28\n")
    with pytest.raises(uploads.UploadError, match="HTTP 500"):
        uploads.put_file_chunked("127.0.0.1", cc2_server, "/upload", f, "a.gcode", "123456")


@pytest.mark.asyncio
async def test_cc2_upload_uses_port_80_and_default_token_without_access_code(tmp_path, monkeypatch):
    from printers.cc2 import CC2Connection
    seen = {}

    def fake(host, port, path, lp, name, token, **kw):
        seen.update(host=host, port=port, path=path, name=name, token=token)
        return {"error_code": 0}
    monkeypatch.setattr(uploads, "put_file_chunked", fake)
    f = tmp_path / "a.gcode"; f.write_bytes(b"G28\n")
    assert await CC2Connection("pid", "10.0.0.9", "CC2").upload_file(f, "a.gcode") is True
    assert seen == {"host": "10.0.0.9", "port": 80, "path": "/upload", "name": "a.gcode", "token": "123456"}
    seen.clear()
    p = CC2Connection("pid", "10.0.0.9", "CC2", access_code="98765")
    await p.upload_file(f, "a.gcode")
    assert seen["token"] == "98765"
