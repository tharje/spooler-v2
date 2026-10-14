"""Many viewers share one connection to the printer's camera."""
import http.server
import threading
import time

import camera_hub

FRAME = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"


class _Cam(http.server.BaseHTTPRequestHandler):
    connections = 0
    open_now = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).connections += 1
        type(self).open_now += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
            self.end_headers()
            while True:
                self.wfile.write(b"--f\r\n\r\n" + FRAME + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            type(self).open_now -= 1


def _serve():
    _Cam.connections = 0
    _Cam.open_now = 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Cam)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/mjpeg"


def test_three_viewers_use_one_printer_connection_and_it_closes_after():
    srv, url = _serve()
    try:
        subs = [camera_hub.hubs.subscribe(url) for _ in range(3)]
        for s in subs:
            assert s.next(timeout=5) == FRAME
        assert _Cam.connections == 1
        for s in subs:
            s.close()
        deadline = time.monotonic() + 5
        while _Cam.open_now and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _Cam.open_now == 0
    finally:
        srv.shutdown()
