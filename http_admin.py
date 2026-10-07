"""
Push, import, diagnostics, upload, backup/restore, features, integrations.

Mixin for http_handler.SPHandler; split out of http_handler.py unchanged.
"""

import http_common
import asyncio
import config
import diagnostics
import json
import notifiers
import notify
import os
import state
import tempfile
import threading
import time
import uploads
import urllib.error
import urllib.parse
import urllib.request
from backup import (
    BACKUP_DIR, RestoreError, create_backup_zip, list_auto_backups, restore_from_zip,
)
from features import FeatureError
from features import describe_all as describe_all_features, is_enabled, requires_feature, set_enabled
from pathlib import Path
from push import WEBPUSH_AVAILABLE, add_subscription, has_subscriptions, remove_subscription, save_notif_settings, send_push_all
from spoolman import get_spoolman_db, get_spoolman_url, test_spoolman_connection

_uploads_lock = threading.Lock()
_uploads_active: set = set()  # printer ids with an upload in flight


class AdminMixin:
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
            if http_common.ws_loop() is None:
                self._json({"error": "Server not ready"}, 503)
                return
            fut = asyncio.run_coroutine_threadsafe(
                printer.upload_file(local_path, name, start_after), http_common.ws_loop())
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
        # HTTP request thread -- http_common.ws_loop() is the same loop reference the combo
        # WebSocket adopter below already uses to bridge threads safely.
        if http_common.ws_loop() is not None:
            asyncio.run_coroutine_threadsafe(
                state.broadcast_to_browsers({"type": "features_changed", "features": features}),
                http_common.ws_loop(),
            )

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
