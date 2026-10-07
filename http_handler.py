"""
HTTP request handler: static files + /api/ routes.
"""

import asyncio
import http.client
import json
import re
import ssl
import socket
import subprocess
import tempfile
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
from backup import (
    BACKUP_DIR, RestoreError, create_backup_zip, list_auto_backups, restore_from_zip,
)
from features import describe_all as describe_all_features, is_enabled, requires_feature, set_enabled
from features import FeatureError
from persistence import DATA_DIR, current_version, load_history, save_printers, snapshot_path, update_history_entry
from push import (
    WEBPUSH_AVAILABLE, add_subscription, get_public_key, has_subscriptions,
    load_notif_settings, remove_subscription, save_notif_settings, send_push_all,
)
from spoolman import get_spool_index, get_spoolman_db, get_spoolman_url, spoolman_auth_header, test_spoolman_connection
import config
import diagnostics
import api_tokens
import ledger
import notifiers
import notify
import stats
import uploads

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

    @requires_feature("camera")
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

    def _stats_params(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        one = lambda k: (q.get(k) or [""])[0].strip() or None
        return one("from"), one("to"), one("printer")

    def _ledger_changed(self) -> None:
        ledger.kick()
        if _ws_loop is not None:
            asyncio.run_coroutine_threadsafe(
                state.broadcast_to_browsers({"type": "ledger_update", **ledger.summary()}), _ws_loop)

    @requires_feature("spoolman")
    def _handle_ledger_list(self):
        """Deductions not (yet) taken by Spoolman, plus recent ones that were
        discarded, so it is visible when filament use wasn't counted."""
        names = {p.id: p.name for p in state.printers.values()}
        files = {e.get("id"): e.get("filename") for e in load_history()}
        cutoff = time.time() - 30 * 86400
        items = []
        for e in ledger.all_entries():
            keep = e["status"] in ledger.UNSENT or (e["status"] == "discarded" and e.get("created_at", 0) >= cutoff)
            if not keep:
                continue
            items.append({**e, "printer_name": names.get(e.get("printer_id")) or e.get("location"),
                          "filename": files.get(e.get("history_id"))})
        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        self._json({"summary": ledger.summary(), "items": items})

    @requires_feature("spoolman")
    def _handle_ledger_action(self, action: str):
        self._read_body()
        rest = urllib.parse.unquote(self.path[len("/api/spoolman-ledger/"):].split("?")[0])
        eid = rest[:-len("/retry")] if action == "retry" else rest
        ok = ledger.retry_now(eid) if action == "retry" else ledger.discard(eid, "Discarded by the user")
        if not ok:
            self._json({"error": "No such deduction waiting to be sent"}, 404)
            return
        self._ledger_changed()
        self._json({"ok": True})

    def _handle_create_api_token(self):
        try:
            body = json.loads(self._read_body() or b"{}")
            key, record = api_tokens.create(str(body.get("name", "")), str(body.get("scope", "read")))
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        self._json({"key": key, "token": record})      # the only time the key is ever shown

    def _apply_reference(self, entry_id: str, body) -> tuple:
        """Shared by the browser and the external API: set one print's
        reference number. Returns (status, payload)."""
        if not isinstance(body, dict) or set(body) - {"reference"}:
            return 400, {"error": "Only 'reference' can be changed"}
        try:
            fields = {"reference": stats.clean_reference(body.get("reference"))}
        except ValueError as e:
            return 400, {"error": str(e)}
        if not entry_id or not update_history_entry(entry_id, fields):
            return 404, {"error": "No such print"}
        if _ws_loop is not None:
            asyncio.run_coroutine_threadsafe(
                state.broadcast_to_browsers({"type": "history_update", "id": entry_id, "fields": fields}),
                _ws_loop,
            )
        return 200, {"ok": True, **fields}

    @requires_feature("statistics")
    def _handle_history_update(self):
        """Edit the user-owned fields of one print. Only `reference` for now."""
        entry_id = urllib.parse.unquote(self.path[len("/api/history/"):].split("?")[0])
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        code, payload = self._apply_reference(entry_id, body)
        self._json(payload, code)

    # ── External API (other programs, API-key auth) ──────────────────────────

    def _external_entry(self, entry: dict, query: dict) -> dict:
        """One print for the external API, with spool names looked up in
        Spoolman (when it's reachable) and the stored entry attached on request."""
        index = get_spool_index() if entry.get("spools") and is_enabled("spoolman") else None
        include = (query.get("include") or [""])[0].split(",")
        return stats.public_entry(entry, spool_index=index, include_raw="raw" in include)

    def _handle_external(self, method: str) -> None:
        self._cors = True
        base = "/api/external/v1"
        if not is_enabled("external_api"):
            self._json({"error": "feature_disabled", "feature": "external_api"}, 403)
            return
        client = self.client_address[0]
        if api_tokens.is_blocked(client):
            self._json({"error": "Too many failed attempts, try again later"}, 429)
            return
        header = self.headers.get("Authorization", "")
        key = header[7:].strip() if header[:7].lower() == "bearer " else ""
        owner = api_tokens.verify(key)
        if owner is None:
            api_tokens.record_failure(client)
            body = json.dumps({"error": "Missing or invalid API key (send 'Authorization: Bearer <key>')"}).encode()
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="spooler"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path).rstrip("/")
        q = urllib.parse.parse_qs(parsed.query)
        one = lambda k: (q.get(k) or [""])[0].strip() or None
        m = re.fullmatch(re.escape(base) + r"/history/([0-9a-f]{32})(/picture)?", path)

        if method == "PATCH":
            if not m or m.group(2):
                self._json({"error": "Not found"}, 404)
                return
            if owner["scope"] != "write":
                self._json({"error": "This key is read-only"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._json({"error": "Bad Content-Length"}, 400)
                return
            if length > EXTERNAL_MAX_BODY:
                self._json({"error": "Request body too large"}, 413)
                return
            try:
                body = json.loads(self.rfile.read(length) if length else b"{}")
            except Exception:
                self._json({"error": "Bad request"}, 400)
                return
            code, payload = self._apply_reference(m.group(1), body)
            self._json(payload, code)
            return

        if path == base:
            self._json({"name": "Spooler external API", "version": 1, "scope": owner["scope"],
                        "endpoints": ["GET /history", "GET /history/{id}", "GET /history/{id}/picture",
                                      "PATCH /history/{id}", "GET /stats", "GET /printers"]})
        elif path == f"{base}/history":
            try:
                limit = min(500, max(1, int(one("limit") or 50)))
                offset = max(0, int(one("offset") or 0))
            except ValueError:
                self._json({"error": "limit and offset must be numbers"}, 400)
                return
            result = one("result")
            if result not in (None, "complete", "cancelled", "error"):
                self._json({"error": "result must be complete, cancelled or error"}, 400)
                return
            order = one("order")
            if order not in (None, "asc", "desc"):
                self._json({"error": "order must be asc or desc"}, 400)
                return
            try:
                rows = stats.query_history(load_history(), one("from"), one("to"), one("printer"),
                                           one("q"), one("reference"), result, one("ended_after"),
                                           oldest_first=(order == "asc"))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            page = rows[offset:offset + limit]
            self._json({"total": len(rows), "limit": limit, "offset": offset,
                        "items": [self._external_entry(e, q) for e in page]})
        elif path == f"{base}/printers":
            self._json({"items": [{"id": p.id, "name": p.name, "type": p.printer_type, "connected": p.connected,
                                   "state": p.to_dict().get("state")} for p in state.printers.values()]})
        elif path == f"{base}/stats":
            self._json(stats.compute_stats(load_history(), one("from"), one("to"), one("printer")))
        elif m:
            entry = next((e for e in load_history() if e.get("id") == m.group(1)), None)
            if entry is None:
                self._json({"error": "No such print"}, 404)
            elif not m.group(2):
                self._json(self._external_entry(entry, q))
            else:
                picture = snapshot_path(m.group(1)) if entry.get("snapshot") else None
                try:
                    data = picture.read_bytes() if picture else None
                except OSError:
                    data = None
                if data is None:
                    self._json({"error": "No picture for that print"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        else:
            self._json({"error": "Not found"}, 404)

    @requires_feature("statistics")
    def _handle_stats(self):
        lo, hi, printer = self._stats_params()
        self._json(stats.compute_stats(load_history(), lo, hi, printer))

    @requires_feature("statistics")
    def _handle_history_csv(self):
        lo, hi, printer = self._stats_params()
        data = stats.history_csv(load_history(), lo, hi, printer)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="spooler-history.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    @requires_feature("print_snapshot")
    def _handle_snapshot(self):
        entry_id = urllib.parse.unquote(self.path[len("/api/snapshot/"):].split("?")[0])
        path = snapshot_path(entry_id)
        try:
            data = path.read_bytes() if path else None
        except OSError:
            data = None
        if data is None:
            self._json({"error": "No picture for that print"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # ── Thumbnail proxy ───────────────────────────────────────────────────────
    # CC1: http://{ip}:80/thumbnail/{bare_filename}  (no auth needed)
    # CC2: no accessible thumbnail endpoint — skipped client-side

    @requires_feature("camera")
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

    @requires_feature("spoolman")
    def _proxy_spoolman(self, method: str, path: str, body: bytes | None):
        if not path.startswith("/api/v1/"):
            self._json({"error": "Invalid Spoolman path"}, 400)
            return
        try:
            headers = spoolman_auth_header()
            if body:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                f"{get_spoolman_url()}{path}",
                data=body,
                headers=headers,
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

    @requires_feature("spoolman")
    def _proxy_spoolman_ui(self, method: str, sm_path: str, body: bytes | None = None):
        """Proxy Spoolman's own web UI through our server.

        Rewrites absolute asset paths in HTML responses so they stay within our
        /spoolman/ prefix (required because Spoolman uses paths like /assets/...).
        """
        try:
            headers = spoolman_auth_header()
            if body:
                headers["Content-Type"] = "application/json"
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
                # Injected before Spoolman's own scripts to fix proxied-subpath issues:
                # 1. Strip /spoolman from URL on load so React Router sees / and matches.
                # 2. Click-intercept on href="/" forces reload via /spoolman/ (Home fix).
                # 3. Patch fetch/XHR so API/asset requests route via /spoolman/.
                # 4. MutationObserver fixes dynamically-rendered <img> src attrs.
                # 5. Unregister SW + clear its caches (wrong scope, bypasses our patches).
                # Note: Spoolman's WebSocket live-push connections are not proxied —
                # they will fail silently; all page data still loads via HTTP polling.
                _patch = (
                    "<script>(function(){"
                    # Strip /spoolman prefix from URL before React Router initialises
                    "if(window.location.pathname.startsWith('/spoolman')){"
                    "history.replaceState(null,'',"
                    "window.location.pathname.slice('/spoolman'.length)||'/');"
                    "}"
                    # Intercept Home link clicks — reload via /spoolman/ so our script
                    # re-runs and React Router sees / correctly.
                    "document.addEventListener('click',function(e){"
                    "var el=e.target&&e.target.closest('a');"
                    "if(el&&(el.getAttribute('href')==='/'||el.pathname==='/')&&"
                    "el.origin===location.origin){"
                    "e.preventDefault();e.stopPropagation();"
                    "window.location.replace('/spoolman/');}},true);"
                    # Rewrite helper for fetch/XHR/img
                    "function _rw(u){return(typeof u==='string'&&u.startsWith('/')&&"
                    "!u.startsWith('/spoolman')&&!u.startsWith('/api/')&&!u.startsWith('/ws'))"
                    "?'/spoolman'+u:u;}"
                    # Patch fetch
                    "var _f=window.fetch;"
                    "window.fetch=function(u,o){return _f.call(this,_rw(u),o);};"
                    # Patch XHR
                    "var _o=XMLHttpRequest.prototype.open;"
                    "XMLHttpRequest.prototype.open=function(m,u){"
                    "arguments[1]=_rw(u);return _o.apply(this,arguments);};"
                    # MutationObserver for dynamically-rendered <img>/<link> elements
                    "var _mo=new MutationObserver(function(ms){"
                    "ms.forEach(function(m){m.addedNodes.forEach(function(n){"
                    "if(n.nodeType!==1)return;"
                    "var els=[n].concat(Array.from(n.querySelectorAll('img,link')));"
                    "els.forEach(function(el){"
                    "var s=el.getAttribute('src');if(s&&el.tagName==='IMG')el.src=_rw(s);"
                    "var h=el.getAttribute('href');if(h&&el.tagName==='LINK')el.href=_rw(h);"
                    "});});});});"
                    "_mo.observe(document,{childList:true,subtree:true});"
                    # Unregister SW and clear its caches so stale SW doesn't intercept
                    "if('serviceWorker'in navigator){"
                    "navigator.serviceWorker.getRegistrations&&"
                    "navigator.serviceWorker.getRegistrations().then(function(rs){"
                    "rs.forEach(function(r){r.unregister();});});"
                    "'caches'in window&&caches.keys().then(function(ns){"
                    "ns.forEach(function(n){caches.delete(n);});});"
                    "navigator.serviceWorker.register=function(){"
                    "return Promise.resolve({scope:'/'});};"
                    "}"
                    "})();</script>"
                )
                html = html.replace("</head>", _patch + "</head>", 1)
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

    @requires_feature("notify_webpush")
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

    @requires_feature("notify_webpush")
    def _handle_push_unsubscribe(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        remove_subscription(body.get("endpoint", ""))
        self._json({"ok": True})

    @requires_feature("notifications")
    def _handle_notif_settings(self):
        try:
            s = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        save_notif_settings(s)
        self._json({"ok": True})

    @requires_feature("notify_webpush")
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

    @requires_feature("spoolman")
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

    @requires_feature("report_problem")
    def _handle_diagnostics(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        data = diagnostics.build_report(qs.get("printer", [""])[0]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="spooler-diagnostics.txt"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    @requires_feature("file_upload")
    def _handle_upload(self):
        """POST /api/upload/<printer_id>?name=<file>&start=0|1 with the raw
        file as the request body. The body is streamed to DATA_DIR/uploads/
        in chunks, then handed to the printer connection."""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        printer_id = urllib.parse.unquote(parsed.path[len("/api/upload/"):])
        printer = state.printers.get(printer_id)
        if not printer:
            self._json({"error": "Printer not found"}, 404)
            return
        if not printer.supports_upload:
            self._json({"error": printer.upload_unsupported_reason}, 400)
            return
        if not printer.connected:
            self._json({"error": "Printer is offline"}, 409)
            return
        start_after = params.get("start", ["0"])[0] in ("1", "true")
        if start_after and printer.to_dict().get("state") not in ("idle", "complete", "cancelled"):
            self._json({"error": "Printer is busy; upload without starting the print."}, 409)
            return
        with _uploads_lock:
            if printer_id in _uploads_active:
                self._json({"error": "An upload to this printer is already in progress"}, 409)
                return
            _uploads_active.add(printer_id)
        local_path = None
        try:
            name = uploads.sanitize_filename(params.get("name", [""])[0])
            length = int(self.headers.get("Content-Length", 0))
            try:
                local_path = uploads.save_stream(self.rfile, length, name)
            except uploads.UploadError as e:
                # An oversized/invalid body was not read; the connection can't
                # be reused safely.
                self.close_connection = True
                self._json({"error": str(e)}, 413 if "too large" in str(e) else 400)
                return
            uploads.on_file_uploaded(printer_id, local_path, name)
            if _ws_loop is None:
                self._json({"error": "Server not ready"}, 503)
                return
            fut = asyncio.run_coroutine_threadsafe(
                printer.upload_file(local_path, name, start_after), _ws_loop)
            ok = fut.result(timeout=uploads.forward_timeout(local_path.stat().st_size) + 30)
            if ok:
                self._json({"ok": True, "filename": name, "started": start_after})
            else:
                self._json({"error": "Upload succeeded but starting the print failed",
                            "filename": name}, 502)
        except uploads.UploadError as e:
            self._json({"error": str(e)}, 400 if str(e).startswith(("Invalid", "Unsupported")) else 502)
        except Exception as e:
            print(f"[Upload] {printer_id} failed: {e}")
            self._json({"error": f"Upload failed: {e}"}, 500)
        finally:
            if local_path is not None:
                local_path.unlink(missing_ok=True)
            with _uploads_lock:
                _uploads_active.discard(printer_id)

    # ── Backup / restore ─────────────────────────────────────────────────────

    @requires_feature("backup")
    def _handle_backup_download(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        include_secrets = params.get("include_secrets", ["0"])[0].lower() in ("1", "true", "yes")
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".zip")
        os.close(tmp_fd)
        try:
            manifest = create_backup_zip(Path(tmp_name), include_secrets=include_secrets)
            data = Path(tmp_name).read_bytes()
        except Exception as e:
            self._json({"error": f"Backup failed: {e}"}, 500)
            return
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        date = time.strftime("%Y-%m-%d")
        filename = f"spooler-backup-{manifest['spooler_version']}-{date}.zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @requires_feature("backup")
    def _handle_backup_file_download(self):
        # Only ever resolve by exact match against what list_auto_backups()
        # itself already reports -- the requested name is never joined onto
        # a filesystem path, so a "../.." in it just fails to match anything.
        name = urllib.parse.unquote(self.path[len("/api/backups/"):].split("?")[0])
        if name not in {b["name"] for b in list_auto_backups()}:
            self._json({"error": "Not found"}, 404)
            return
        data = (BACKUP_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @requires_feature("backup")
    def _handle_restore(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json({"error": "No file uploaded"}, 400)
            return
        body = self._read_body()
        if body is None:
            return  # _read_body() already sent a 413 if the body was too large
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".zip")
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(body)
            manifest = restore_from_zip(Path(tmp_name))
        except RestoreError as e:
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:
            self._json({"error": f"Restore failed: {e}"}, 500)
            return
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        self._json({
            "ok": True,
            "manifest": manifest,
            "message": "Restored. Spooler is restarting — reload this page in a few seconds.",
        })
        # In-memory state (loaded printer connections and their live asyncio
        # tasks, the cached VAPID key, auth sessions) can't be safely swapped
        # out from under a running process -- a controlled restart is the
        # robust way to pick up the restored files everywhere. Delayed so the
        # response above actually reaches the client first. Docker's
        # restart: unless-stopped brings the container back automatically;
        # outside Docker the process needs restarting by hand.
        threading.Timer(1.0, lambda: os._exit(0)).start()

    # ── Feature flags ─────────────────────────────────────────────────────────

    def _handle_patch_features(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        key = body.get("key")
        enabled = body.get("enabled")
        if not isinstance(key, str) or not isinstance(enabled, bool):
            self._json({"error": "Expected {\"key\": str, \"enabled\": bool}"}, 400)
            return
        try:
            cascaded = set_enabled(key, enabled)
        except FeatureError as e:
            self._json({"error": str(e)}, 400)
            return
        features = describe_all_features()
        self._json({"ok": True, "features": features, "cascaded": cascaded})
        # Broadcasting runs on the asyncio loop, this handler runs on its own
        # HTTP request thread -- _ws_loop is the same loop reference the combo
        # WebSocket adopter below already uses to bridge threads safely.
        if _ws_loop is not None:
            asyncio.run_coroutine_threadsafe(
                state.broadcast_to_browsers({"type": "features_changed", "features": features}),
                _ws_loop,
            )

    # ── Integrations config ──────────────────────────────────────────────────

    def _handle_patch_integrations(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        values = body.get("values", {})
        clear_keys = body.get("clear", [])
        if not isinstance(values, dict) or not isinstance(clear_keys, list):
            self._json({"error": 'Expected {"values": {...}, "clear": [...]}'}, 400)
            return

        errors = {}
        for key, value in values.items():
            field = config.FIELDS.get(key)
            if field is None:
                errors[key] = "Unknown field"
                continue
            if field.type == "secret" and value == "":
                continue  # blank secret field on save means "leave unchanged"
            try:
                config.set(key, value)
            except config.ConfigError as e:
                errors[key] = str(e)
        for key in clear_keys:
            try:
                config.clear(key)
            except config.ConfigError as e:
                errors[key] = str(e)

        if errors:
            self._json({"error": "Some fields could not be updated", "fields": errors}, 400)
            return
        self._json({"ok": True, "fields": config.describe_all()})

    def _handle_test_notifier(self, channel: str):
        self._read_body()
        feature = notifiers.CHANNELS[channel][0]
        if not is_enabled(feature):
            self._json({"error": "feature_disabled", "feature": feature}, 403)
            return
        try:
            notify.send_test(channel)
            result = {"ok": True, "message": "Test message sent."}
        except notifiers.NotifierError as e:
            result = {"ok": False, "message": str(e)}
        config.record_test_result(channel, result["ok"], result["message"])
        self._json(result)

    @requires_feature("spoolman")
    def _handle_test_spoolman(self):
        self._read_body()
        result = test_spoolman_connection()
        config.record_test_result("spoolman", result["ok"], result["message"])
        self._json(result)

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
