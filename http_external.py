"""
Stats, history, Spoolman-ledger, API-token and external-API routes.

Mixin for http_handler.SPHandler; split out of http_handler.py unchanged.
"""

import http_common
import api_tokens
import asyncio
import json
import ledger
import re
import state
import stats
import time
import urllib.error
import urllib.parse
import urllib.request
from features import is_enabled, requires_feature
from persistence import load_history, snapshot_path, update_history_entry
from spoolman import get_spool_index


EXTERNAL_MAX_BODY = 16 * 1024  # the external API only ever takes a few bytes of JSON


class ExternalRoutesMixin:
    def _stats_params(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        one = lambda k: (q.get(k) or [""])[0].strip() or None
        return one("from"), one("to"), one("printer")

    def _ledger_changed(self) -> None:
        ledger.kick()
        if http_common.ws_loop() is not None:
            asyncio.run_coroutine_threadsafe(
                state.broadcast_to_browsers({"type": "ledger_update", **ledger.summary()}), http_common.ws_loop())

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
        ok = ledger.retry_now(eid) if action == "retry" else (ledger.discard(eid, "Discarded by the user") or ledger.dismiss(eid))
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
        if http_common.ws_loop() is not None:
            asyncio.run_coroutine_threadsafe(
                state.broadcast_to_browsers({"type": "history_update", "id": entry_id, "fields": fields}),
                http_common.ws_loop(),
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
