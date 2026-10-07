"""
Camera, thumbnail and Spoolman proxies.

Mixin for http_handler.SPHandler; split out of http_handler.py unchanged.
"""

import http.client
import state
import urllib.error
import urllib.parse
import urllib.request
from features import requires_feature
from spoolman import get_spoolman_url, spoolman_auth_header




class ProxyMixin:
    @requires_feature("camera")
    def _proxy_camera(self):
        printer_id = urllib.parse.unquote(self.path[len("/api/camera/"):].split("?")[0])
        p = state.printers.get(printer_id)
        if not p or not p.camera_url:
            print(f"[Camera] {p.name if p else printer_id}: no camera address known (yet)")
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
                print(f"[Camera] {p.name}: the printer's camera answered HTTP {upstream.status}")
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
            pass    # the browser closed the picture
        except Exception as e:
            # Said once per failure so "no camera feed" can be told apart from a printer that won't serve it.
            print(f"[Camera] {p.name}: could not read the printer's camera ({type(e).__name__}: {e})")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

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
