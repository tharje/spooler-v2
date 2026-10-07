"""
HTTP request handler: static files + /api/ routes.
"""

import asyncio
import json
import ssl
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import os
import state
from auth import (
    AUTH_ENABLED, BCRYPT_AVAILABLE, SECURE_COOKIES, SESSION_COOKIE,
    _auth_ok, _check_rate_limit, _create_session, _get_pw_hash, _get_username,
    _has_password, _invalidate_session, _parse_sid, _reset_rate_limit, _save_auth,
)
from backup import list_auto_backups
from features import describe_all as describe_all_features, is_enabled
from persistence import DATA_DIR, current_version, load_history
from push import WEBPUSH_AVAILABLE, get_public_key, load_notif_settings
from spoolman import get_spoolman_db, get_spoolman_url
import http_common
from http_external import ExternalRoutesMixin
from http_proxy import ProxyMixin
from http_admin import AdminMixin
import config
import api_tokens
import notifiers

try:
    import bcrypt as _bcrypt
except ImportError:
    _bcrypt = None  # type: ignore

CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE  = DATA_DIR / "key.pem"

MAX_BODY = 100 * 1024 * 1024  # 100 MB
EXTERNAL_MAX_BODY = 16 * 1024  # the external API only ever takes a few bytes of JSON
_uploads_lock = threading.Lock()
_uploads_active: set = set()  # printer ids with an upload in flight

_current_version = current_version  # kept as a module-local alias; call sites unchanged

def proxy_spoolman_enabled() -> bool:
    # Read live (not a module constant) so a change in Settings -> Integrations
    # takes effect immediately -- every call site below already calls this
    # function rather than checking a cached value.
    return config.get("spoolman.proxy")


def _server_settings_readonly() -> dict:
    """Server network/auth settings -- deliberately never editable from the
    UI (a wrong value here could lock the user out), shown read-only in
    Settings -> Integrations alongside the fields that ARE editable there."""
    return {
        "HTTP_PORT":     os.getenv("HTTP_PORT", "8080"),
        "HTTPS_PORT":    os.getenv("HTTPS_PORT", "8443"),
        "WS_PORT":       os.getenv("WS_PORT", "8765"),
        "WSS_PORT":      os.getenv("WSS_PORT", "8766"),
        "HTTPS_ENABLED": os.getenv("HTTPS_ENABLED", "true"),
        "AUTH_ENABLED":  os.getenv("AUTH_ENABLED", "true"),
        "DATA_DIR":      str(DATA_DIR),
    }


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


class SPHandler(ExternalRoutesMixin, ProxyMixin, AdminMixin, SimpleHTTPRequestHandler):
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

    # ── External API (other programs, API-key auth) ──────────────────────────

    # ── Thumbnail proxy ───────────────────────────────────────────────────────
    # CC1: http://{ip}:80/thumbnail/{bare_filename}  (no auth needed)
    # CC2: no accessible thumbnail endpoint — skipped client-side

    # ── Spoolman proxy ────────────────────────────────────────────────────────

    # ── HTTP verbs ────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path.startswith("/api/external/"):
            self._handle_external("GET")
            return
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
        if self.path == "/config.js":
            ws_port  = int(os.getenv("WS_PORT",  "8765"))
            wss_port = int(os.getenv("WSS_PORT", "8766"))
            ws_host  = os.getenv("WS_HOST", "")
            body = (
                f"window.SPOOLER_WS_PORT  = {ws_port};\n"
                f"window.SPOOLER_WSS_PORT = {wss_port};\n"
                f"window.SPOOLER_WS_HOST  = {json.dumps(ws_host)};\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        # Spoolman — either proxy through our server or redirect directly to it
        if self.path in ("/spoolman", "/spoolman/"):
            if proxy_spoolman_enabled():
                if self.path == "/spoolman":
                    self.send_response(302)
                    self.send_header("Location", "/spoolman/")
                    self.end_headers()
                else:
                    self._proxy_spoolman_ui("GET", "/")
            else:
                self.send_response(302)
                self.send_header("Location", get_spoolman_url() + "/")
                self.end_headers()
            return
        if proxy_spoolman_enabled() and self.path.startswith("/spoolman/"):
            sm_path = self.path[len("/spoolman"):]
            self._proxy_spoolman_ui("GET", sm_path or "/")
            return
        # Spoolman's own JS calls /api/v1/ directly — only proxy when enabled
        if proxy_spoolman_enabled() and self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("GET", self.path)
            return
        if self.path == "/api/auth-status":
            resp = {"setup_required": not _has_password()}
            if _auth_ok(self):
                resp["spoolman_url"] = "/spoolman/" if proxy_spoolman_enabled() else get_spoolman_url() + "/"
            self._json(resp)
            return

        if self.path == "/api/health":
            # No auth required — this is what Docker's HEALTHCHECK and external
            # monitoring hit, which won't carry a session cookie. Only reports
            # connection liveness, nothing sensitive (no IPs, no access codes).
            import time as _time
            self._json({
                "status":   "ok",
                "version":  _current_version(),
                "uptime_s": round(_time.time() - state.START_TIME),
                "printers": [
                    {
                        "id":        p.id,
                        "name":      p.name,
                        "type":      p.printer_type,
                        "connected": p.connected,
                    }
                    for p in state.printers.values()
                ],
            })
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
            if not is_enabled("notifications"):
                self._json({"error": "feature_disabled", "feature": "notifications"}, 403)
                return
            self._json(load_notif_settings())
            return

        if not self._check_auth():
            return

        if self.path == "/api/features":
            self._json(describe_all_features())
        elif self.path.split("?")[0] == "/api/diagnostics":
            self._handle_diagnostics()
        elif self.path == "/api/integrations":
            self._json({
                "fields": config.describe_all(),
                "tests": {k: config.last_test_result(k) for k in ("spoolman", *notifiers.CHANNELS)},
                "server_settings": _server_settings_readonly(),
            })
        elif self.path == "/api/api-tokens":
            self._json({"tokens": api_tokens.list_tokens(), "max": api_tokens.MAX_TOKENS})
        elif self.path == "/api/spoolman-ledger":
            self._handle_ledger_list()
        elif self.path == "/api/security-status":
            import auth
            self._json({"auth_enabled": bool(auth.AUTH_ENABLED)})
        elif self.path.split("?")[0] == "/api/stats":
            self._handle_stats()
        elif self.path.split("?")[0] == "/api/history.csv":
            self._handle_history_csv()
        elif self.path.startswith("/api/snapshot/"):
            self._handle_snapshot()
        elif self.path.startswith("/api/camera/"):
            self._proxy_camera()
        elif self.path.startswith("/api/thumbnail/"):
            self._proxy_thumbnail()
        elif self.path == "/api/printers":
            self._json([p.to_dict() for p in state.printers.values()])
        elif self.path == "/api/history":
            self._json(load_history())
        elif self.path.startswith("/api/lookup-ean"):
            if not is_enabled("spoolman"):
                self._json({"error": "feature_disabled", "feature": "spoolman"}, 403)
                return
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
            if not is_enabled("spoolman"):
                self._json({"error": "feature_disabled", "feature": "spoolman"}, 403)
                return
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
        elif self.path == "/api/backup" or self.path.startswith("/api/backup?"):
            self._handle_backup_download()
        elif self.path == "/api/backups":
            if not is_enabled("backup"):
                self._json({"error": "feature_disabled", "feature": "backup"}, 403)
                return
            self._json(list_auto_backups())
        elif self.path.startswith("/api/backups/"):
            self._handle_backup_file_download()
        elif self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("GET", self.path[len("/api/spoolman"):], None)
        elif (
            proxy_spoolman_enabled()
            and "text/html" in self.headers.get("Accept", "")
            and self.path not in ("/", "")
            and not Path(self.path.split("?")[0]).suffix
        ):
            # Spoolman SPA sub-route (e.g. /spools after client-side navigation + refresh)
            self._proxy_spoolman_ui("GET", "/")
        else:
            # Root/directory paths fall through to index.html via super().
            # Static assets not in public/ are proxied to Spoolman when enabled
            # (handles favicon, kofi logo, etc. rendered without /spoolman/ prefix).
            path_part = self.path.lstrip("/").split("?")[0] or "index.html"
            local = Path(__file__).parent / "public" / path_part
            if local.is_file():
                super().do_GET()
            elif proxy_spoolman_enabled() and Path(path_part).suffix:
                self._proxy_spoolman_ui("GET", self.path.split("?")[0])
            else:
                super().do_GET()

    def do_PATCH(self):
        if self.path.startswith("/api/external/"):
            self._handle_external("PATCH")
            return
        if not self._check_auth():
            return
        if self.path == "/api/features":
            self._handle_patch_features()
            return
        if self.path == "/api/integrations":
            self._handle_patch_integrations()
            return
        if self.path.startswith("/api/history/"):
            self._handle_history_update()
            return
        if proxy_spoolman_enabled() and self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("PATCH", self.path, self._read_body())
            return
        if self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("PATCH", self.path[len("/api/spoolman"):], self._read_body())
        else:
            self._json({"error": "Not found"}, 404)

    def do_DELETE(self):
        if not self._check_auth():
            return
        if self.path.startswith("/api/spoolman-ledger/"):
            self._handle_ledger_action("discard")
            return
        if self.path.startswith("/api/api-tokens/"):
            ok = api_tokens.revoke(urllib.parse.unquote(self.path[len("/api/api-tokens/"):]))
            self._json({"ok": True} if ok else {"error": "No such key"}, 200 if ok else 404)
            return
        if proxy_spoolman_enabled() and self.path.startswith("/api/v1/"):
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
        elif self.path == "/api/api-tokens":
            self._handle_create_api_token()
        elif self.path.startswith("/api/spoolman-ledger/") and self.path.endswith("/retry"):
            self._handle_ledger_action("retry")
        elif self.path == "/api/import-filaments":
            self._handle_import_filaments()
        elif self.path == "/api/integrations/spoolman/test":
            self._handle_test_spoolman()
        elif self.path.startswith("/api/integrations/") and self.path.endswith("/test") \
                and self.path.split("/")[3] in notifiers.CHANNELS:
            self._handle_test_notifier(self.path.split("/")[3])
        elif self.path == "/api/restore":
            self._handle_restore()
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ── Complex POST handlers ─────────────────────────────────────────────────

    # ── Backup / restore ─────────────────────────────────────────────────────

    # ── Feature flags ─────────────────────────────────────────────────────────

    # ── Integrations config ──────────────────────────────────────────────────

    # ── Response helper ───────────────────────────────────────────────────────

    def _json(self, data, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "_cors", False):
            self.send_header("Access-Control-Allow-Origin", "*")   # token-only API: no cookies involved
        self.end_headers()
        self.wfile.write(body)


# ── WebSocket-on-the-same-port ("combo") ─────────────────────────────────────
#
# The browser WebSocket normally lives on its own port (WS_PORT/WSS_PORT).
# That's unreachable through single-port reverse proxies / tunnels (e.g. a
# Cloudflare Tunnel hostname mapped to just http://<ip>:8080), so the plain
# HTTP server here can also adopt WebSocket upgrade requests and hand the raw
# socket off to the asyncio WS server — same origin, same port, no extra
# tunnel/proxy config needed. HTTPS is not combo'd: TLS must be terminated
# before the request line is visible, so PWA installs over the self-signed
# cert on HTTPS_PORT keep using the separate WSS_PORT.

_ws_loop = None
_ws_connection_factory = None


def set_ws_adopter(loop, connection_factory) -> None:
    """Register the asyncio loop + WS connection factory that combo mode
    hands adopted sockets to. Must be called before run_http()'s server
    starts accepting connections."""
    global _ws_loop, _ws_connection_factory
    _ws_loop = loop
    http_common.set_ws_loop(loop)
    _ws_connection_factory = connection_factory


def _looks_like_ws_upgrade(sock, timeout: float = 1.0) -> bool:
    """Peek (non-destructively) at the start of a freshly accepted connection
    to see if it's an HTTP WebSocket upgrade request, without consuming any
    bytes — so the request is still intact for whichever handler takes it."""
    deadline = time.time() + timeout
    sock.settimeout(0.2)
    data = b""
    try:
        while time.time() < deadline:
            try:
                data = sock.recv(4096, socket.MSG_PEEK)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data or b"\r\n\r\n" in data or len(data) > 1024:
                break
    finally:
        sock.settimeout(None)
    head = data.split(b"\r\n\r\n", 1)[0].lower()
    return b"upgrade" in head and b"websocket" in head


def _adopt_ws_socket(sock, client_address) -> None:
    async def _do():
        try:
            await _ws_loop.connect_accepted_socket(_ws_connection_factory, sock)
        except Exception as e:
            print(f"[WS] Combo adopt failed for {client_address}: {e}")
            try:
                sock.close()
            except Exception:
                pass
    asyncio.run_coroutine_threadsafe(_do(), _ws_loop)


class ComboHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that also accepts WebSocket upgrades on the same
    port (see module note above)."""

    def process_request(self, request, client_address):
        if _ws_loop is None:
            super().process_request(request, client_address)
            return
        # _looks_like_ws_upgrade() blocks for up to ~1s on a slow/silent
        # client. process_request() runs on the server's single accept-loop
        # thread (socketserver dispatches to a new thread *inside*
        # ThreadingHTTPServer.process_request, not before it), so peeking
        # here directly would stall accepting any other connection while it
        # waits. Hand off to a thread immediately instead, same as
        # ThreadingMixIn normally does for the HTTP-only path.
        t = threading.Thread(target=self._route_request, args=(request, client_address), daemon=True)
        t.start()

    def _route_request(self, request, client_address):
        if _looks_like_ws_upgrade(request):
            _adopt_ws_socket(request, client_address)
            return
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def run_http(port: int) -> None:
    server = ComboHTTPServer(("0.0.0.0", port), SPHandler)
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
