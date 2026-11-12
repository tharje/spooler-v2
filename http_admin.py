"""
Push, import, diagnostics, upload, backup/restore, features, integrations.

Mixin for http_handler.SPHandler; split out of http_handler.py unchanged.
"""

import http_common
import asyncio
import config
import diagnostics
import job_queue
import json
import notifiers
import notify
import service
import dataset
import spaghetti
import update_check
import os
import state
import tempfile
import threading
import time
import uploads
import user_channels
import users
import urllib.error
import urllib.parse
import urllib.request
from auth import current_user
from backup import (
    BACKUP_DIR, RestoreError, create_backup_zip, list_auto_backups, restore_from_zip,
)
from features import FeatureError
from features import describe_all as describe_all_features, is_enabled, requires_feature, set_enabled
from pathlib import Path
import push
from push import WEBPUSH_AVAILABLE, add_subscription, has_subscriptions, remove_subscription, save_notif_settings, send_push_all
from snapshot import grab_jpeg
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
        add_subscription(sub, owner=current_user(self))
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

    @requires_feature("notifications")
    def _handle_my_notifications_get(self):
        user = current_user(self)
        own = push.user_event_choices(user) if user else None
        shared = push.load_notif_settings()
        keys = sorted(set(notify.EVENT_SETTING.values()))
        self._json({
            "user": user,
            "custom": own is not None,
            # What this user's devices get now: their own choices, or the shared ones.
            "events": {k: push.user_wants(user, k, shared) for k in keys} if user else
                      {k: bool((shared.get(k) or {}).get("enabled")) for k in keys},
        })

    @requires_feature("notifications")
    def _handle_my_notifications_set(self):
        """Save the signed-in user's own event choices ({"events": {key: bool}},
        or {"events": null} to follow the shared settings again)."""
        user = current_user(self)
        if not user:
            self._json({"error": "Sign in with an account to choose your own notifications."}, 400)
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        events = body.get("events")
        if events is not None and not isinstance(events, dict):
            self._json({"error": "events must be an object"}, 400)
            return
        valid = set(notify.EVENT_SETTING.values())
        push.save_user_event_choices(user, None if events is None else
                                     {k: bool(v) for k, v in events.items() if k in valid})
        self._json({"ok": True})

    def _my_channels_user(self) -> str | None:
        """The signed-in user, when they may have own channels (sends the error otherwise)."""
        user = current_user(self)
        if not user:
            self._json({"error": "Sign in with an account to set up your own channels."}, 400)
            return None
        if not users.has_permission(user, user_channels.PERMISSION):
            self._json({"error": "forbidden", "need": user_channels.PERMISSION}, 403)
            return None
        return user

    @requires_feature("notifications")
    def _handle_my_channels_get(self):
        user = self._my_channels_user()
        if user:
            self._json({"channels": user_channels.describe(user)})

    @requires_feature("notifications")
    def _handle_my_channels_update(self):
        """{"channel": "telegram", "enabled": true, "values": {...}, "clear": [...]}"""
        user = self._my_channels_user()
        if not user:
            return
        try:
            body = json.loads(self._read_body() or b"{}")
            values, clear = body.get("values") or {}, body.get("clear") or []
            if not isinstance(values, dict) or not isinstance(clear, list):
                raise ValueError("Bad request.")
            enabled = body.get("enabled")
            user_channels.update(user, str(body.get("channel", "")),
                                 None if enabled is None else bool(enabled), values, clear)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        self._json({"ok": True, "channels": user_channels.describe(user)})

    @requires_feature("notifications")
    def _handle_my_channel_test(self, channel: str):
        self._read_body()
        user = self._my_channels_user()
        if not user:
            return
        vals = (user_channels.channels_of(user).get(channel) or {}).get("values") or {}
        if not user_channels.is_configured(channel, vals):
            msg = ("Spooler has no shared Telegram bot yet: ask an admin to set one up, or enter your own bot token."
                   if channel == "telegram" and vals.get("telegram.chat_id") else "Fill in the channel's settings first.")
            self._json({"ok": False, "message": msg}, 400)
            return
        try:
            notify.send_test(channel, notifiers.user_getter(vals, channel))
            self._json({"ok": True, "message": "Sent. Check your " + channel + "."})
        except notifiers.NotifierError as e:
            self._json({"ok": False, "message": str(e)}, 502)

    @requires_feature("notify_webpush")
    def _handle_push_test(self):
        self._read_body()
        if not WEBPUSH_AVAILABLE:
            self._json({"error": "Web Push not available on server"}, 503)
            return
        # Only to the tester's own devices ("" = login off: every device).
        owner = current_user(self)
        if not has_subscriptions(owner):
            self._json({"error": "No push subscriptions registered"}, 400)
            return
        send_push_all("Spooler — Test notification", "Push notifications are working!", owner=owner)
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
        if not printer or not self._can_see_printer(printer_id):
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
            if start_after:
                printer._pending_owner = current_user(self)   # attribute the print in history
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

    @requires_feature("job_queue")
    def _handle_queue_list(self):
        self._json({"jobs": job_queue.list_jobs()})

    @requires_feature("job_queue")
    def _handle_queue_upload(self):
        """POST /api/queue/upload?name=<file>&printer_type=&printer_id=&material=&colour=&quantity=&priority=
        with the raw file as the request body -- same streaming pattern as
        _handle_upload, but the file goes into the queue instead of straight
        to a printer."""
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        local_path = None
        try:
            name = uploads.sanitize_filename(params.get("name", [""])[0])
            length = int(self.headers.get("Content-Length", 0))
            try:
                local_path = uploads.save_stream(self.rfile, length, name)
            except uploads.UploadError as e:
                self.close_connection = True
                self._json({"error": str(e)}, 413 if "too large" in str(e) else 400)
                return
            requirements = {k: params.get(k, [""])[0] for k in
                             ("printer_type", "printer_id", "material", "colour", "quantity", "priority")}
            pid = (requirements.get("printer_id") or "").strip()
            if pid and not self._can_see_printer(pid):
                self._json({"error": "Printer not found"}, 404)
                return
            requirements["owner"] = current_user(self)
            job = job_queue.add_job(local_path, name, requirements)
            local_path = None  # add_job() moved it into the queue folder
            self._json({"ok": True, "job": job})
        except (uploads.UploadError, job_queue.QueueError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            print(f"[Queue] upload failed: {e}")
            self._json({"error": f"Could not add to the queue: {e}"}, 500)
        finally:
            if local_path is not None:
                local_path.unlink(missing_ok=True)

    @requires_feature("job_queue")
    def _handle_queue_from_printer(self):
        """POST /api/queue/from-printer {printer_id, filename, quantity?, priority?,
        material?, colour?} -- queue a file that's already on that printer."""
        try:
            body = json.loads(self._read_body() or b"{}")
            printer_id = (body.get("printer_id") or "").strip()
            filename = (body.get("filename") or "").strip()
            if not printer_id or not filename:
                self._json({"error": "printer_id and filename are required"}, 400)
                return
            if not self._can_see_printer(printer_id):
                self._json({"error": "Printer not found"}, 404)
                return
            body["owner"] = current_user(self)
            job = job_queue.add_existing_job(printer_id, filename, body)
            self._json({"ok": True, "job": job})
        except job_queue.QueueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"Could not add to the queue: {e}"}, 500)

    @requires_feature("job_queue")
    def _handle_queue_update(self):
        """PATCH /api/queue/<job id> {priority?, quantity?, status: "cancelled"?}"""
        job_id = urllib.parse.unquote(self.path[len("/api/queue/"):])
        try:
            changes = json.loads(self._read_body() or b"{}")
            if not self._may_touch_job(job_id, cancelling=changes.get("status") == "cancelled"):
                return
            job = job_queue.update_job(job_id, changes)
            self._json({"ok": True, "job": job})
        except job_queue.QueueError as e:
            self._json({"error": str(e)}, 404 if "not found" in str(e).lower() else 400)
        except Exception as e:
            self._json({"error": f"Could not update the job: {e}"}, 500)

    @requires_feature("job_queue")
    def _handle_queue_delete(self):
        job_id = urllib.parse.unquote(self.path[len("/api/queue/"):])
        if not self._may_touch_job(job_id, cancelling=True):
            return
        try:
            job_queue.delete_job(job_id)
            self._json({"ok": True})
        except job_queue.QueueError as e:
            self._json({"error": str(e)}, 404)

    def _may_touch_job(self, job_id: str, cancelling: bool) -> bool:
        """Authorise cancelling/removing a queued job by its owner. Reordering or
        re-prioritising (not cancelling) needs the general queue.manage permission;
        cancelling needs job.cancel_own (for your own) or job.cancel_any. Sends the
        error response and returns False when not allowed. No-ops (returns True)
        when login is off or the request carries no identity (single-admin/tests)."""
        from auth import AUTH_ENABLED
        user = current_user(self)
        if not AUTH_ENABLED or not user:
            return True
        job = job_queue.get_job(job_id)
        if job is None:
            self._json({"error": "Job not found"}, 404)
            return False
        if cancelling:
            if users.can_cancel_job(user, job.get("owner")):
                return True
        elif users.has_permission(user, "queue.manage"):
            return True
        self._json({"error": "forbidden",
                    "need": "job.cancel_any" if cancelling else "queue.manage"}, 403)
        return False

    @requires_feature("job_queue")
    def _handle_queue_confirm(self):
        """POST /api/queue/<printer id>/confirm -- the user has cleared the
        plate; upload and start this printer's assigned job now."""
        printer_id = urllib.parse.unquote(self.path[len("/api/queue/"):-len("/confirm")])
        if not self._can_see_printer(printer_id):
            self._json({"error": "Printer not found"}, 404)
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                job_queue.confirm_plate_cleared(printer_id), http_common.ws_loop())
            job = fut.result(timeout=120)
            self._json({"ok": True, "job": job})
        except job_queue.QueueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            print(f"[Queue] confirm failed for {printer_id}: {e}")
            self._json({"error": f"Could not start the print: {e}"}, 500)

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

    @requires_feature("service_log")
    def _handle_service_get(self):
        self._json(service.overview({p.id: {"name": p.name, "type": p.printer_type}
                                      for p in state.printers.values()}))

    @requires_feature("service_log")
    def _handle_service_post(self, what: str):
        try:
            body = json.loads(self._read_body() or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        try:
            if what == "hours":
                item = {"hours": service.set_print_hours(body.get("printer_id"), body.get("hours"))}
            elif what == "tasks":
                item = service.add_task(body.get("printer_id"), body.get("name"), body.get("interval"),
                                    body.get("kind") or "other", body.get("unit") or "hours")
            else:
                item = service.add_log(body.get("printer_id"), body.get("kind"), body.get("note"),
                                       body.get("date"), body.get("task_id"))
        except service.ServiceError as e:
            self._json({"error": str(e)}, 400)
            return
        self._json({"ok": True, "item": item})

    @requires_feature("service_log")
    def _handle_service_delete(self):
        parts = self.path.split("?")[0].split("/")          # ['', 'api', 'service', 'tasks'|'log', id]
        what, item_id = (parts[3], parts[4]) if len(parts) == 5 else ("", "")
        ok = service.delete_task(item_id) if what == "tasks" else service.delete_log(item_id) if what == "log" else False
        self._json({"ok": True} if ok else {"error": "Not found"}, 200 if ok else 404)

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

    @requires_feature("update_check")
    def _handle_update_skip(self):
        try:
            body = json.loads(self._read_body() or b"{}")
            update_check.skip(str(body.get("version") or ""))
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        self._json({"ok": True})

    @requires_feature("dataset_collection")
    def _handle_dataset_label(self):
        try:
            body = json.loads(self._read_body() or b"{}")
            printer = state.printers.get(body.get("printer_id"))
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        if printer is None:
            self._json({"error": "Unknown printer"}, 404)
            return
        try:
            dataset.label_now(printer, body.get("label"))
        except dataset.DatasetError as e:
            self._json({"error": str(e)}, 409)
            return
        self._json({"ok": True})

    @requires_feature("spoolman")
    @requires_feature("spaghetti_detection")
    def _handle_test_spaghetti(self):
        self._read_body()
        jpeg = None
        for p in list(state.printers.values()):          # a real camera picture is the better test
            if p.camera_url and p.connected:
                jpeg = grab_jpeg(p.camera_url)
                if jpeg:
                    break
        result = spaghetti.test_connection(jpeg)
        config.record_test_result("spaghetti", result["ok"], result["message"])
        self._json(result)

    def _handle_test_spoolman(self):
        self._read_body()
        result = test_spoolman_connection()
        config.record_test_result("spoolman", result["ok"], result["message"])
        self._json(result)

    # ── Users & roles (RBAC admin) ───────────────────────────────────────────

    def _handle_users_list(self):
        self._json({"users": users.list_users(), "current": current_user(self)})

    def _handle_users_create(self):
        try:
            body = json.loads(self._read_body() or b"{}")
            u = users.create_user(body.get("username", ""), body.get("password", ""),
                                  body.get("role", ""), printers=body.get("printers"))
            self._json({"ok": True, "user": u})
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _handle_users_update(self, username: str):
        try:
            body = json.loads(self._read_body() or b"{}")
            if body.get("password"):
                users.set_password(username, body["password"])
            if body.get("role"):
                users.set_role(username, body["role"])
            if "printers" in body:
                users.set_user_printers(username, body["printers"])
            self._json({"ok": True, "user": users.get_user(username)})
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _handle_users_delete(self, username: str):
        try:
            users.delete_user(username)
            push.remove_owner(username)
            user_channels.remove_user(username)
            self._json({"ok": True})
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _handle_roles_list(self):
        self._json({"roles": users.list_roles(),
                    "permissions": [{"key": k, "label": lbl} for k, lbl in users.PERMISSIONS]})

    def _handle_roles_create(self):
        try:
            body = json.loads(self._read_body() or b"{}")
            r = users.create_role(body.get("name", ""), body.get("permissions", []),
                                  body.get("label", ""),
                                  printers=body.get("printers", users.ALL_PRINTERS))
            self._json({"ok": True, "role": r})
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _handle_roles_update(self, name: str):
        try:
            body = json.loads(self._read_body() or b"{}")
            users.update_role(name, permissions=body.get("permissions"),
                              label=body.get("label"), printers=body.get("printers"))
            self._json({"ok": True})
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _handle_roles_delete(self, name: str):
        try:
            users.delete_role(name)
            self._json({"ok": True})
        except ValueError as e:
            self._json({"error": str(e)}, 400)
