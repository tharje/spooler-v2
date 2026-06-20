"""
HTTP request handler: static files + /api/ routes.
"""

import http.client
import json
import ssl
import socket
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import state
from auth import (
    AUTH_ENABLED, BCRYPT_AVAILABLE, SECURE_COOKIES, SESSION_COOKIE,
    _auth_ok, _check_rate_limit, _create_session, _get_pw_hash, _get_username,
    _has_password, _invalidate_session, _parse_sid, _reset_rate_limit, _save_auth,
)
from persistence import DATA_DIR, load_history, save_printers
from push import (
    WEBPUSH_AVAILABLE, add_subscription, get_public_key, has_subscriptions,
    load_notif_settings, remove_subscription, save_notif_settings, send_push_all,
)
from spoolman import get_spoolman_db, get_spoolman_url

try:
    import bcrypt as _bcrypt
except ImportError:
    _bcrypt = None  # type: ignore

CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE  = DATA_DIR / "key.pem"

MAX_BODY = 100 * 1024 * 1024  # 100 MB


def ensure_ssl_cert() -> bool:
    if CERT_FILE.exists() and KEY_FILE.exists():
        return True
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
                "-days", "3650", "-nodes",
                "-subj", "/CN=spooler.local",
                "-addext", f"subjectAltName=IP:{local_ip},IP:127.0.0.1,DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )
        print(f"[SSL] Certificate generated (IP: {local_ip})")
        return True
    except Exception as e:
        print(f"[SSL] Could not generate certificate: {e}")
        return False


class SPHandler(SimpleHTTPRequestHandler):
    """Serve static files from ./public and handle /api/ calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent / "public"), **kwargs)

    def log_message(self, fmt, *args):
        pass  # silence access log

    # ── Auth helpers ─────────────────────────────────────────────────────────

    def _check_auth(self) -> bool:
        if _auth_ok(self):
            return True
        if self.path.startswith("/api/"):
            self._json({"error": "Unauthorized"}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
        return False

    def _session_cookie(self, token: str, clear: bool = False) -> str:
        is_secure = SECURE_COOKIES or getattr(self.server, "_is_https", False)
        if clear:
            value = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        else:
            value = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        if is_secure:
            value += "; Secure"
        return value

    def _read_body(self) -> bytes | None:
        raw = int(self.headers.get("Content-Length", 0))
        if raw > MAX_BODY:
            self._json({"error": "Request body too large"}, 413)
            return None
        return self.rfile.read(raw) if raw else None

    def _handle_login(self):
        ip = self.client_address[0]
        if not _check_rate_limit(ip):
            self._json({"error": "Too many login attempts, try again later"}, 429)
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        username = body.get("username", "")
        password = body.get("password", "")

        ok = False
        pw_hash = _get_pw_hash()
        if AUTH_ENABLED and BCRYPT_AVAILABLE and _bcrypt and pw_hash and username == _get_username():
            try:
                ok = _bcrypt.checkpw(password.encode(), pw_hash.encode())
            except Exception:
                pass

        if not ok:
            self._json({"error": "Invalid credentials"}, 401)
            return

        _reset_rate_limit(ip)
        token = _create_session()
        resp = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Set-Cookie", self._session_cookie(token))
        self.end_headers()
        self.wfile.write(resp)

    def _handle_logout(self):
        token = _parse_sid(self.headers.get("Cookie", ""))
        _invalidate_session(token)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.send_header("Set-Cookie", self._session_cookie("", clear=True))
        self.end_headers()
        self.wfile.write(b"{}")

    def _handle_setup(self):
        if _has_password():
            self._json({"error": "Already configured"}, 403)
            return
        if not BCRYPT_AVAILABLE or not _bcrypt:
            self._json({"error": "bcrypt not installed on server"}, 500)
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        username = body.get("username", "").strip()
        password = body.get("password", "")
        if not username or not password:
            self._json({"error": "Username and password are required"}, 400)
            return
        if len(password) < 8:
            self._json({"error": "Password must be at least 8 characters"}, 400)
            return
        try:
            pw_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
            _save_auth(username, pw_hash)
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        token = _create_session()
        resp = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Set-Cookie", self._session_cookie(token))
        self.end_headers()
        self.wfile.write(resp)
        print(f"[Auth] Initial password set for user '{username}'")

    def _handle_change_password(self):
        if not BCRYPT_AVAILABLE or not _bcrypt:
            self._json({"error": "bcrypt not installed on server"}, 500)
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        password = body.get("password", "")
        if len(password) < 8:
            self._json({"error": "Password must be at least 8 characters"}, 400)
            return
        try:
            pw_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
            from auth import _load_auth
            auth_data = _load_auth()
            username = auth_data.get("username", "admin")
            _save_auth(username, pw_hash)
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"ok": True})
        print(f"[Auth] Password changed for user '{username}'")

    # ── Camera proxy ──────────────────────────────────────────────────────────

    def _proxy_camera(self):
        printer_id = urllib.parse.unquote(self.path[len("/api/camera/"):].split("?")[0])
        p = state.printers.get(printer_id)
        if not p or not p.camera_url:
            self._json({"error": "Camera not available"}, 404)
            return
        conn = None
        try:
            parsed = urllib.parse.urlparse(p.camera_url)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
            conn.request("GET", path)
            upstream = conn.getresponse()
            if upstream.status != 200:
                self._json({"error": f"camera returned {upstream.status}"}, 503)
                return
            try:
                conn.sock.settimeout(None)  # no per-read timeout — stream runs indefinitely
            except Exception:
                pass
            ct = upstream.getheader("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    # ── Thumbnail proxy ───────────────────────────────────────────────────────
    # CC1: http://{ip}:80/thumbnail/{bare_filename}  (no auth needed)
    # CC2: no accessible thumbnail endpoint — skipped client-side

    def _proxy_thumbnail(self):
        rest = self.path[len("/api/thumbnail/"):]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            self._json({"error": "Bad request"}, 400)
            return
        printer_id = urllib.parse.unquote(parts[0])
        bare_name  = urllib.parse.unquote(parts[1])
        p = state.printers.get(printer_id)
        if not p:
            self._json({"error": "Printer not found"}, 404)
            return

        token = p.access_code or "123456"

        # CC2 has no accessible thumbnail endpoint — caller should not request one
        if p.printer_type == "cc2":
            self._json({"error": "Thumbnail unavailable"}, 404)
            return

        # CC1: dedicated /thumbnail/{bare_filename} endpoint (no auth required)
        conn = None
        try:
            path = f"/thumbnail/{urllib.parse.quote(bare_name)}"
            conn = http.client.HTTPConnection(p.ip, 80, timeout=5)
            conn.request("GET", path, headers={})
            resp = conn.getresponse()
            if state.DEBUG:
                print(f"[thumb] {bare_name!r} → {resp.status}")
            if resp.status == 200:
                data = resp.read(2 * 1024 * 1024)
                ct   = resp.getheader("Content-Type", "image/png")
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=300")
                self.end_headers()
                self.wfile.write(data)
                return
        except Exception as e:
            if state.DEBUG:
                print(f"[thumb] exception for {bare_name!r}: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        self._json({"error": "Thumbnail unavailable"}, 404)

    # ── Spoolman proxy ────────────────────────────────────────────────────────

    def _proxy_spoolman(self, method: str, path: str, body: bytes | None):
        if not path.startswith("/api/v1/"):
            self._json({"error": "Invalid Spoolman path"}, 400)
            return
        try:
            req = urllib.request.Request(
                f"{get_spoolman_url()}{path}",
                data=body,
                headers={"Content-Type": "application/json"} if body else {},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data   = resp.read()
                status = resp.status
            if not data:
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
            else:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self._json({"error": str(e)}, e.code)
        except Exception as e:
            self._json({"error": f"Spoolman unreachable: {e}"}, 502)

    def _proxy_spoolman_ui(self, method: str, sm_path: str, body: bytes | None = None):
        """Proxy Spoolman's own web UI through our server.

        Rewrites absolute asset paths in HTML responses so they stay within our
        /spoolman/ prefix (required because Spoolman uses paths like /assets/...).
        """
        try:
            headers = {"Content-Type": "application/json"} if body else {}
            req = urllib.request.Request(
                f"{get_spoolman_url()}{sm_path}",
                data=body,
                headers=headers,
                method=method,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                status = resp.status
                ct = resp.headers.get("Content-Type", "application/octet-stream")

            # Rewrite absolute paths in HTML so assets load through our proxy
            if "text/html" in ct:
                html = data.decode("utf-8", errors="replace")
                html = html.replace('src="/', 'src="/spoolman/')
                html = html.replace("src='/", "src='/spoolman/")
                html = html.replace('href="/', 'href="/spoolman/')
                html = html.replace("href='/", "href='/spoolman/")
                data = html.encode("utf-8")
                ct = "text/html; charset=utf-8"

            self.send_response(status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            body_err = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_err)))
            self.end_headers()
            self.wfile.write(body_err)
        except Exception as e:
            self._json({"error": f"Spoolman unreachable: {e}"}, 502)

    # ── HTTP verbs ────────────────────────────────────────────────────────────

    def do_GET(self):
        # Auth-exempt routes
        if self.path == "/cert.pem":
            data = CERT_FILE.read_bytes() if CERT_FILE.exists() else b""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-pem-file")
            self.send_header("Content-Disposition", 'attachment; filename="spooler-cert.pem"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path in ("/login", "/login.html"):
            login_file = Path(__file__).parent / "public" / "login.html"
            data = login_file.read_bytes() if login_file.exists() else b"Login page not found"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path in ("/manifest.json", "/icon.svg", "/sw.js", "/favicon.ico"):
            super().do_GET()
            return
        # Spoolman UI proxy — must come before auth gate so the browser can
        # load assets without cookie (crossorigin script tags don't send cookies)
        if self.path == "/spoolman":
            self.send_response(302)
            self.send_header("Location", "/spoolman/")
            self.end_headers()
            return
        if self.path.startswith("/spoolman/"):
            sm_path = self.path[len("/spoolman"):]  # e.g. "/assets/..."
            self._proxy_spoolman_ui("GET", sm_path or "/")
            return
        # Spoolman's own JS calls /api/v1/ directly — proxy those through
        if self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("GET", self.path)
            return
        if self.path == "/api/auth-status":
            resp = {"setup_required": not _has_password()}
            if _auth_ok(self):
                resp["spoolman_url"] = "/spoolman/"
            self._json(resp)
            return

        if self.path == "/api/push-public-key":
            # Public key is safe to expose without auth — it's literally a public key.
            # Service workers need it to renew expired subscriptions without a session cookie.
            if WEBPUSH_AVAILABLE and get_public_key():
                self._json({"publicKey": get_public_key()})
            else:
                self._json({"error": "Web Push not available"}, 503)
            return

        if self.path == "/api/notification-settings":
            if not self._check_auth():
                return
            self._json(load_notif_settings())
            return

        if not self._check_auth():
            return

        if self.path.startswith("/api/camera/"):
            self._proxy_camera()
        elif self.path.startswith("/api/thumbnail/"):
            self._proxy_thumbnail()
        elif self.path == "/api/printers":
            self._json([p.to_dict() for p in state.printers.values()])
        elif self.path == "/api/history":
            self._json(load_history())
        elif self.path.startswith("/api/lookup-ean"):
            qs     = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            ean    = params.get("ean", [""])[0].strip()
            if not ean:
                self._json({"error": "ean required"}, 400)
                return
            db = get_spoolman_db()
            for item in db:
                if ean in (item.get("ean") or []):
                    self._json(item)
                    return
            self._json({"error": "Not found"}, 404)
        elif self.path == "/api/filament-meta":
            db     = get_spoolman_db()
            brands = sorted({item.get("manufacturer", "") for item in db if item.get("manufacturer")})
            mat_map: dict = {}
            for item in db:
                mat = item.get("material") or ""
                den = item.get("density")
                if mat and mat not in mat_map and den:
                    mat_map[mat] = den
            materials = [{"name": m, "density": mat_map[m]} for m in sorted(mat_map)]
            self._json({"brands": brands, "materials": materials})
        elif self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("GET", self.path[len("/api/spoolman"):], None)
        else:
            super().do_GET()

    def do_PATCH(self):
        if not self._check_auth():
            return
        if self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("PATCH", self.path, self._read_body())
            return
        if self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("PATCH", self.path[len("/api/spoolman"):], self._read_body())
        else:
            self._json({"error": "Not found"}, 404)

    def do_DELETE(self):
        if not self._check_auth():
            return
        if self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("DELETE", self.path)
            return
        if self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("DELETE", self.path[len("/api/spoolman"):], None)
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        # Auth-exempt
        if self.path == "/api/login":
            self._handle_login()
            return
        if self.path == "/api/logout":
            self._handle_logout()
            return
        if self.path == "/api/setup":
            self._handle_setup()
            return

        if not self._check_auth():
            return

        if self.path == "/api/change-password":
            self._handle_change_password()
        elif self.path == "/api/push-subscribe":
            self._handle_push_subscribe()
        elif self.path == "/api/push-unsubscribe":
            self._handle_push_unsubscribe()
        elif self.path == "/api/notification-settings":
            self._handle_notif_settings()
        elif self.path == "/api/push-test":
            self._handle_push_test()
        elif self.path == "/api/import-filaments":
            self._handle_import_filaments()
        elif self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("POST", self.path, self._read_body())
        elif self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("POST", self.path[len("/api/spoolman"):], self._read_body())
        elif self.path.startswith("/api/upload/"):
            self._handle_upload()
        else:
            self._json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── Complex POST handlers ─────────────────────────────────────────────────

    def _handle_push_subscribe(self):
        try:
            sub = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        endpoint = sub.get("endpoint", "")
        if not endpoint:
            self._json({"error": "Missing endpoint"}, 400)
            return
        add_subscription(sub)
        self._json({"ok": True})

    def _handle_push_unsubscribe(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        remove_subscription(body.get("endpoint", ""))
        self._json({"ok": True})

    def _handle_notif_settings(self):
        try:
            s = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        save_notif_settings(s)
        self._json({"ok": True})

    def _handle_push_test(self):
        self._read_body()
        if not WEBPUSH_AVAILABLE:
            self._json({"error": "Web Push not available on server"}, 503)
            return
        if not has_subscriptions():
            self._json({"error": "No push subscriptions registered"}, 400)
            return
        send_push_all("Spooler — Test notification", "Push notifications are working!")
        self._json({"ok": True})

    def _handle_import_filaments(self):
        try:
            req_body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        brand = req_body.get("brand", "").strip()
        if not brand:
            self._json({"error": "brand required"}, 400)
            return
        db      = get_spoolman_db()
        entries = [f for f in db if f.get("manufacturer", "").lower() == brand.lower()]
        if not entries:
            self._json({"error": f"No filaments found for '{brand}'"}, 404)
            return
        try:
            vurl = f"{get_spoolman_url()}/api/v1/vendor?name={urllib.parse.quote(brand)}"
            with urllib.request.urlopen(vurl, timeout=5) as r:
                vendors = json.loads(r.read())
            if vendors:
                vendor_id = vendors[0]["id"]
            else:
                vreq = urllib.request.Request(
                    f"{get_spoolman_url()}/api/v1/vendor",
                    data=json.dumps({"name": brand}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(vreq, timeout=5) as r:
                    vendor_id = json.loads(r.read())["id"]
        except Exception as e:
            self._json({"error": f"Vendor error: {e}"}, 500)
            return
        try:
            with urllib.request.urlopen(
                f"{get_spoolman_url()}/api/v1/filament?limit=10000", timeout=5
            ) as r:
                existing = {f["article_number"] for f in json.loads(r.read()) if f.get("article_number")}
        except Exception:
            existing = set()
        created = skipped = 0
        for f in entries:
            article = f.get("id", "")
            if article and article in existing:
                skipped += 1
                continue
            body: dict = {
                "vendor_id": vendor_id,
                "material":  f.get("material", ""),
                "density":   f.get("density")  or 1.24,
                "diameter":  f.get("diameter") or 1.75,
                "weight":    f.get("weight")   or 1000,
            }
            if f.get("name"):          body["name"]                  = f["name"]
            if f.get("spool_weight"):  body["spool_weight"]           = f["spool_weight"]
            if f.get("color_hex"):     body["color_hex"]              = f["color_hex"].lstrip("#")
            if f.get("extruder_temp"): body["settings_extruder_temp"] = f["extruder_temp"]
            if f.get("bed_temp"):      body["settings_bed_temp"]      = f["bed_temp"]
            if article:                body["article_number"]         = article
            try:
                freq = urllib.request.Request(
                    f"{get_spoolman_url()}/api/v1/filament",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(freq, timeout=5) as r:
                    r.read()
                created += 1
            except Exception:
                skipped += 1
        print(f"[Import] {brand}: {created} created, {skipped} skipped")
        self._json({"brand": brand, "created": created, "skipped": skipped, "total": len(entries)})

    def _handle_upload(self):
        printer_id = urllib.parse.unquote(self.path[len("/api/upload/"):])
        printer    = state.printers.get(printer_id)
        if not printer:
            self._json({"error": "Printer not found"}, 404)
            return
        ct = self.headers.get("Content-Type", "")
        if not ct.startswith("multipart/form-data"):
            self._json({"error": "Expected multipart/form-data"}, 400)
            return
        body = self._read_body()
        try:
            req = urllib.request.Request(
                f"http://{printer.ip}/uploadFile/upload",
                data=body,
                headers={"Content-Type": ct},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = resp.read()
            self._json({"ok": True, "response": result.decode("utf-8", errors="replace")})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ── Response helper ───────────────────────────────────────────────────────

    def _json(self, data, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_http(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), SPHandler)
    print(f"[HTTP] Serving on http://0.0.0.0:{port}")
    server.serve_forever()


def run_https(port: int) -> None:
    if not ensure_ssl_cert():
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    server = ThreadingHTTPServer(("0.0.0.0", port), SPHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server._is_https = True
    print(f"[HTTPS] Serving on https://0.0.0.0:{port}")
    server.serve_forever()
