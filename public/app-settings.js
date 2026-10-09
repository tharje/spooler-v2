/* Spooler frontend: Settings, push, backup, notification channels, printers panel. Plain scripts sharing one global scope; load order is in index.html. */
"use strict";

// ─── Sign out ─────────────────────────────────────────────────────────────────
async function signOut() {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  location.replace("/login");
}
document.getElementById("btn-signout")?.addEventListener("click", signOut);
document.getElementById("btn-signout-nav")?.addEventListener("click", signOut);

// ─── App settings ─────────────────────────────────────────────────────────────
const _settingsModal          = document.getElementById("modal-app-settings");
const _settingsMenu           = document.getElementById("settings-menu");
const _settingsPwPage         = document.getElementById("settings-change-password");
const _settingsNotifPage      = document.getElementById("settings-notifications");
const _settingsPrintersPage   = document.getElementById("settings-printers");
const _settingsPrinterEditPage = document.getElementById("settings-printer-edit");
const _settingsBackupPage     = document.getElementById("settings-backup");
const _settingsFeaturesPage   = document.getElementById("settings-features");
const _settingsIntegrationsPage = document.getElementById("settings-integrations");
const _settingsApiPage = document.getElementById("settings-api");

const _allSettingsPages = () => [_settingsPwPage, _settingsNotifPage, _settingsPrintersPage, _settingsPrinterEditPage, _settingsBackupPage, _settingsFeaturesPage, _settingsIntegrationsPage, _settingsApiPage];

function _openSettings() {
  _allSettingsPages().forEach(p => p && (p.style.display = "none"));
  _settingsMenu.style.display = "";
  const tog = document.getElementById("toggle-light-mode");
  if (tog) tog.checked = document.body.classList.contains("light-mode");
  _settingsModal?.classList.add("open");
}
function _showSettingsPage(pageEl) {
  _settingsMenu.style.display = "none";
  _allSettingsPages().forEach(p => p && (p.style.display = "none"));
  pageEl.style.display = "";
}
function _backToSettingsMenu() {
  _allSettingsPages().forEach(p => p && (p.style.display = "none"));
  _settingsMenu.style.display = "";
}

function _renderSettingsPrinters() {
  const list = document.getElementById("settings-printer-list");
  if (!list) return;
  const items = Object.values(printers);
  if (!items.length) {
    list.innerHTML = '<p class="settings-printer-empty">No printers added yet.</p>';
    return;
  }
  list.innerHTML = items.map(p => `
    <div class="settings-printer-row">
      <div class="settings-printer-info">
        <span class="settings-printer-name">${escHtml(p.name)}</span>
        <span class="settings-printer-meta">${escHtml(p.ip)} &nbsp;·&nbsp; ${p.printer_type?.toUpperCase() || "CC1"}</span>
      </div>
      <button class="btn btn-secondary btn-sm" onclick="_settingsEditPrinter('${escAttr(p.id)}')">Edit</button>
    </div>
  `).join("");
}

let _settingsEditingPrinterId = null;

// Per-printer options (the "Options" box on the printer edit page). Each
// option row has an `data-option-for` list of printer types it applies to;
// rows that don't apply are hidden, and the whole box hides when none apply.
// To add an option: add a .option-row inside #settings-edit-options in
// index.html with data-option-for="cc1 cc2 ..." and read/write it where
// "auto_light" is handled in _settingsEditPrinter and the save handler.
function _syncPrinterOptions(type) {
  const box = document.getElementById("settings-edit-options");
  if (!box) return;
  let any = false;
  box.querySelectorAll(".option-row").forEach(row => {
    const applies = (row.dataset.optionFor || "").split(/\s+/).includes(type);
    row.style.display = applies ? "" : "none";
    any = any || applies;
  });
  box.style.display = any ? "" : "none";
}

function _settingsEditPrinter(id) {
  _settingsEditingPrinterId = id || null;
  const p = id ? printers[id] : null;
  const isNew = !p;

  document.getElementById("settings-printer-edit-title").textContent = isNew ? "Add Printer" : "Edit Printer";
  document.getElementById("settings-edit-name").value = p?.name || "";
  document.getElementById("settings-edit-ip").value = p?.ip || "";

  const typeSelect = document.getElementById("settings-edit-type");
  const typeReadonly = document.getElementById("settings-edit-type-readonly");
  if (isNew) {
    typeSelect.value = "cc1";
    typeSelect.style.display = "";
    typeReadonly.style.display = "none";
  } else {
    typeSelect.value = p.printer_type || "cc1";
    typeSelect.style.display = "none";
    typeReadonly.style.display = "";
    const typeLabels = {
      cc1:       "CC1 – Centauri Carbon 1 (WebSocket/SDCP)",
      cc2:       "CC2 – Centauri Carbon 2 (MQTT)",
      prusa:     "Prusa – PrusaLink HTTP API",
      moonraker: "Klipper – Moonraker HTTP API",
    };
    typeReadonly.textContent = typeLabels[p.printer_type] || typeLabels.cc1;
  }

  const curType = p ? p.printer_type : typeSelect.value;
  const needsKey = curType === "cc2" || curType === "prusa" || curType === "moonraker";
  document.getElementById("settings-edit-access-code-label").style.display = needsKey ? "" : "none";
  document.getElementById("settings-edit-access-code-text").textContent =
    curType === "cc2" ? "MQTT Password" : "API Key";
  const ac = document.getElementById("settings-edit-access-code");
  ac.value = "";
  ac.placeholder = (!isNew && p?.has_access_code)
    ? "Leave blank to keep current"
    : curType === "moonraker" ? "API key (optional)"
    : curType === "prusa" ? "API key from PrusaLink settings" : "12345";

  _syncPrinterOptions(curType);
  document.getElementById("settings-edit-autolight").checked = !!p?.auto_light;
  document.getElementById("btn-settings-printer-remove").style.display = isNew ? "none" : "";
  document.getElementById("btn-settings-printer-save").textContent = isNew ? "Add Printer" : "Save";

  _showSettingsPage(_settingsPrinterEditPage);
}

document.getElementById("btn-app-settings")?.addEventListener("click", _openSettings);
document.getElementById("btn-app-settings-cancel")?.addEventListener("click", () =>
  _settingsModal?.classList.remove("open"));
_settingsModal?.addEventListener("click", e => {
  if (e.target === _settingsModal) _settingsModal.classList.remove("open");
});

// Light mode toggle
(function () {
  const toggle = document.getElementById("toggle-light-mode");
  if (!toggle) return;
  toggle.checked = document.body.classList.contains("light-mode");
  toggle.addEventListener("change", () => {
    document.body.classList.toggle("light-mode", toggle.checked);
    localStorage.setItem("theme", toggle.checked ? "light" : "dark");
  });
})();

// Printers sub-page
document.getElementById("btn-settings-goto-printers")?.addEventListener("click", () => {
  _renderSettingsPrinters();
  _showSettingsPage(_settingsPrintersPage);
});
document.getElementById("btn-settings-back-printers")?.addEventListener("click", _backToSettingsMenu);
document.getElementById("btn-settings-add-printer")?.addEventListener("click", () => {
  _settingsEditPrinter(null);
});
document.getElementById("btn-settings-scan-printers")?.addEventListener("click", () => {
  send({ action: "discover" });
  toast("Scanning for printers…");
});

// Printer edit sub-page
document.getElementById("btn-settings-back-printer-edit")?.addEventListener("click", () => {
  _renderSettingsPrinters();
  _showSettingsPage(_settingsPrintersPage);
});
document.getElementById("settings-edit-type")?.addEventListener("change", function () {
  _syncPrinterOptions(this.value);
  const needsKey = this.value === "cc2" || this.value === "prusa" || this.value === "moonraker";
  document.getElementById("settings-edit-access-code-label").style.display = needsKey ? "" : "none";
  document.getElementById("settings-edit-access-code-text").textContent =
    this.value === "cc2" ? "MQTT Password" : "API Key";
  document.getElementById("settings-edit-access-code").placeholder =
    this.value === "moonraker" ? "API key (optional)"
    : this.value === "prusa" ? "API key from PrusaLink settings" : "12345";
});
document.getElementById("btn-settings-printer-save")?.addEventListener("click", () => {
  const name = document.getElementById("settings-edit-name").value.trim();
  const ip   = document.getElementById("settings-edit-ip").value.trim();
  const access_code = document.getElementById("settings-edit-access-code").value.trim();
  const auto_light = document.getElementById("settings-edit-autolight").checked;
  if (!ip) { toast("IP address is required"); return; }
  if (_settingsEditingPrinterId) {
    send({ action: "update_printer", printer_id: _settingsEditingPrinterId, name, ip, access_code, auto_light });
  } else {
    const printer_type = document.getElementById("settings-edit-type").value;
    send({ action: "add_printer", ip, name: name || undefined, printer_type, access_code, auto_light });
  }
  _renderSettingsPrinters();
  _showSettingsPage(_settingsPrintersPage);
});
document.getElementById("btn-settings-printer-remove")?.addEventListener("click", () => {
  if (!_settingsEditingPrinterId) return;
  if (!confirm("Remove this printer?")) return;
  send({ action: "remove_printer", printer_id: _settingsEditingPrinterId });
  _renderSettingsPrinters();
  _showSettingsPage(_settingsPrintersPage);
});

// Password sub-page
document.getElementById("btn-settings-goto-password")?.addEventListener("click", () => {
  document.getElementById("settings-new-password").value = "";
  document.getElementById("settings-confirm-password").value = "";
  _showSettingsPage(_settingsPwPage);
});
document.getElementById("btn-settings-back")?.addEventListener("click", _backToSettingsMenu);
document.getElementById("btn-app-settings-save")?.addEventListener("click", async () => {
  const pw  = document.getElementById("settings-new-password").value;
  const pw2 = document.getElementById("settings-confirm-password").value;
  if (pw.length < 8)  { toast("Password must be at least 8 characters"); return; }
  if (pw !== pw2)     { toast("Passwords do not match"); return; }
  const r = await fetch("/api/change-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }),
  });
  if (r.ok) {
    toast("Password updated");
    _settingsModal?.classList.remove("open");
  } else {
    const d = await r.json().catch(() => ({}));
    toast(d.error || "Failed to update password");
  }
});

// ─── Push notifications ───────────────────────────────────────────────────────

function _urlBase64ToUint8Array(b64) {
  const pad = "=".repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

async function _subscribePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return null;
  try {
    const reg = await navigator.serviceWorker.ready;
    const keyResp = await fetch("/api/push-public-key");
    if (!keyResp.ok) { console.warn("Push: could not get public key", keyResp.status); return null; }
    const { publicKey } = await keyResp.json();
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: _urlBase64ToUint8Array(publicKey),
    });
    const r = await fetch("/api/push-subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub.toJSON()),
    });
    if (!r.ok) { console.warn("Push: server rejected subscription", r.status); return null; }
    return sub;
  } catch (e) {
    console.warn("Push subscribe failed:", e);
    return null;
  }
}

async function _unsubscribePush() {
  if (!("serviceWorker" in navigator)) return;
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await fetch("/api/push-unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      });
      await sub.unsubscribe();
    }
  } catch (e) {
    console.warn("Push unsubscribe failed:", e);
  }
}

// Events added with T10. Each gets an on/off switch, optionally a camera
// picture switch and one numeric parameter; rendered here instead of as
// hand-written HTML so the list, the form and the save code can't drift apart.
const _EXTRA_NOTIF_EVENTS = [
  { key: "started",         label: "Print started",    desc: "When a print begins", image: true },
  { key: "paused",          label: "Print paused",     desc: "When a print pauses, with the cause (pauses you start from Spooler aren't announced)", image: true },
  { key: "error",           label: "Print error",      desc: "When the printer reports an error, with the cause and code when known", image: true },
  { key: "filament_runout", label: "Filament runout",  desc: "When the printer stops because filament ran out", image: true },
  { key: "firmware",        label: "Firmware changed", desc: "When a printer reports a different firmware version than before (and whether it is tested with Spooler)" },
  { key: "offline",         label: "Printer offline",  desc: "When a printer has been unreachable for a while, and when it comes back",
    param: { name: "minutes", label: "Minutes offline before notifying", def: 5, min: 1, max: 1440 } },
];

function _renderExtraNotifEvents(s) {
  const host = document.getElementById("notif-extra-events");
  if (!host) return;
  host.innerHTML = _EXTRA_NOTIF_EVENTS.map(ev => `
    <div class="notif-row">
      <div class="notif-info">
        <span class="notif-label">${escHtml(ev.label)}</span>
        <span class="notif-desc">${escHtml(ev.desc)}</span>
      </div>
      <label class="toggle-switch">
        <input type="checkbox" id="notif-ev-${ev.key}-on" ${s[ev.key]?.enabled ? "checked" : ""} />
        <span class="toggle-slider"></span>
      </label>
    </div>
    ${ev.image ? `<div class="notif-param"><label class="notif-chip"><input type="checkbox" id="notif-ev-${ev.key}-image" ${s[ev.key]?.image ? "checked" : ""} /><span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg> Include photo</span></label></div>` : ""}
    ${ev.param ? `<div class="notif-param"><label>${escHtml(ev.param.label)}<input type="number" id="notif-ev-${ev.key}-${ev.param.name}" value="${s[ev.key]?.[ev.param.name] ?? ev.param.def}" min="${ev.param.min}" max="${ev.param.max}" /></label></div>` : ""}
  `).join("");
}

function _collectExtraNotifEvents() {
  const out = {};
  for (const ev of _EXTRA_NOTIF_EVENTS) {
    const o = { enabled: document.getElementById(`notif-ev-${ev.key}-on`)?.checked ?? false };
    if (ev.image) o.image = document.getElementById(`notif-ev-${ev.key}-image`)?.checked ?? false;
    if (ev.param) o[ev.param.name] = parseFloat(document.getElementById(`notif-ev-${ev.key}-${ev.param.name}`)?.value) || ev.param.def;
    out[ev.key] = o;
  }
  return out;
}

// Notifications sub-page UI
async function _populateNotifForm() {
  await _reloadChannels(false);
  const resp = await fetch("/api/notification-settings").catch(() => null);
  const s = resp?.ok ? await resp.json().catch(() => ({})) : {};
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el[typeof val === "boolean" ? "checked" : "value"] = val;
  };
  set("notif-finished-on",               s.finished?.enabled        ?? false);
  set("notif-layer-on",                  s.layer?.enabled           ?? false);
  set("notif-layer-number",              s.layer?.layer             ?? 1);
  set("notif-layer-image",               s.layer?.image             ?? false);
  set("notif-nozzle-idle-on",            s.nozzle_idle?.enabled     ?? false);
  set("notif-nozzle-idle-threshold",     s.nozzle_idle?.threshold   ?? 50);
  set("notif-nozzle-printing-on",        s.nozzle_printing?.enabled ?? false);
  set("notif-nozzle-printing-threshold", s.nozzle_printing?.threshold ?? 260);
  set("notif-spool-low-on",              s.spool_low?.enabled       ?? false);
  set("notif-spool-low-threshold",       s.spool_low?.threshold     ?? 100);
  set("notif-finished-image",            s.finished?.image          ?? false);
  _renderExtraNotifEvents(s);
  _syncNotifParams();
}

function _syncNotifParams() {
  const show = (paramId, checkId) => {
    const param = document.getElementById(paramId);
    const cb    = document.getElementById(checkId);
    if (param && cb) param.style.display = cb.checked ? "" : "none";
  };
  show("notif-layer-param",           "notif-layer-on");
  show("notif-nozzle-idle-param",     "notif-nozzle-idle-on");
  show("notif-nozzle-printing-param", "notif-nozzle-printing-on");
  show("notif-spool-low-param",       "notif-spool-low-on");
}

["notif-layer-on","notif-nozzle-idle-on","notif-nozzle-printing-on","notif-spool-low-on"].forEach(id =>
  document.getElementById(id)?.addEventListener("change", _syncNotifParams));

document.getElementById("btn-settings-goto-notifications")?.addEventListener("click", () => {
  _populateNotifForm();
  _showSettingsPage(_settingsNotifPage);
});
document.getElementById("btn-settings-back-notif")?.addEventListener("click", _backToSettingsMenu);

// ─── Backup / restore ───────────────────────────────────────────────────────
async function _renderAutoBackupList() {
  const list = document.getElementById("backup-auto-list");
  if (!list) return;
  list.innerHTML = '<span class="backup-auto-empty">Loading…</span>';
  try {
    const r = await fetch("/api/backups");
    const backups = r.ok ? await r.json() : [];
    if (!backups.length) {
      list.innerHTML = '<span class="backup-auto-empty">No automatic backups yet.</span>';
      return;
    }
    list.innerHTML = backups.map(b => {
      const date = new Date(b.modified * 1000).toLocaleString();
      const kb   = (b.size / 1024).toFixed(0);
      return `<div class="backup-auto-row">
        <span>${escHtml(date)} · ${kb} KB</span>
        <a href="/api/backups/${encodeURIComponent(b.name)}" class="btn btn-secondary btn-sm">Download</a>
      </div>`;
    }).join("");
  } catch (_) {
    list.innerHTML = '<span class="backup-auto-empty">Could not load backups.</span>';
  }
}

function _populateBackupIntervalField() {
  const input = document.getElementById("backup-interval-days");
  if (!input) return;
  const field = integrations.fields.find(f => f.key === "backup.interval_days");
  input.value = field ? field.value : 1;
}

document.getElementById("btn-settings-goto-backup")?.addEventListener("click", async () => {
  _showSettingsPage(_settingsBackupPage);
  _renderAutoBackupList();
  await loadIntegrations();
  _populateBackupIntervalField();
});
document.getElementById("btn-settings-back-backup")?.addEventListener("click", _backToSettingsMenu);

document.getElementById("btn-backup-interval-save")?.addEventListener("click", async () => {
  const input = document.getElementById("backup-interval-days");
  const days = parseInt(input.value, 10);
  if (isNaN(days) || days < 0) {
    toast("Enter a number of days (0 or more)", true);
    return;
  }
  try {
    const r = await fetch("/api/integrations", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: { "backup.interval_days": days } }),
    });
    const data = await r.json();
    if (!r.ok) {
      toast(data.error || "Could not save interval", true);
      return;
    }
    toast(days === 0 ? "Automatic backups disabled" : `Interval set to ${days} day(s)`);
  } catch (e) {
    toast("Could not save interval: " + e.message, true);
  }
});

document.getElementById("btn-settings-goto-features")?.addEventListener("click", () => {
  _showSettingsPage(_settingsFeaturesPage);
  _renderFeaturesList();
});
document.getElementById("btn-settings-back-features")?.addEventListener("click", _backToSettingsMenu);

document.getElementById("btn-settings-goto-integrations")?.addEventListener("click", () => {
  _showSettingsPage(_settingsIntegrationsPage);
  _renderIntegrationsList();
  loadImportBrands();
  _renderServerSettings();
});
document.getElementById("btn-settings-back-integrations")?.addEventListener("click", _backToSettingsMenu);

document.getElementById("btn-backup-download")?.addEventListener("click", () => {
  const includeSecrets = document.getElementById("backup-include-secrets")?.checked;
  const url = `/api/backup${includeSecrets ? "?include_secrets=1" : ""}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
});

document.getElementById("btn-backup-restore")?.addEventListener("click", async () => {
  const input = document.getElementById("backup-restore-file");
  const file = input?.files?.[0];
  if (!file) {
    toast("Choose a backup file first");
    return;
  }
  if (!confirm("This overwrites current printers, history and settings with the contents of this backup, and restarts Spooler. Continue?")) {
    return;
  }
  try {
    const r = await fetch("/api/restore", { method: "POST", body: file });
    const data = await r.json();
    if (!r.ok) {
      toast(data.error || "Restore failed", true);
      return;
    }
    toast(data.message || "Restored — reloading…");
    setTimeout(() => location.reload(), 4000);
  } catch (e) {
    toast("Restore failed: " + e.message, true);
  }
});
document.getElementById("btn-notif-save")?.addEventListener("click", async () => {
  const gb = id => document.getElementById(id)?.checked ?? false;
  const gv = id => parseFloat(document.getElementById(id)?.value) || 0;
  const s = {
    finished:        { enabled: gb("notif-finished-on"), image: gb("notif-finished-image") },
    layer:           { enabled: gb("notif-layer-on"),           layer:     gv("notif-layer-number"), image: gb("notif-layer-image") },
    nozzle_idle:     { enabled: gb("notif-nozzle-idle-on"),     threshold: gv("notif-nozzle-idle-threshold") },
    nozzle_printing: { enabled: gb("notif-nozzle-printing-on"), threshold: gv("notif-nozzle-printing-threshold") },
    spool_low:       { enabled: gb("notif-spool-low-on"),       threshold: gv("notif-spool-low-threshold") },
    ..._collectExtraNotifEvents(),
  };
  // Browser push needs this browser's permission. Other channels (ntfy,
  // Telegram, ...) don't, so a refusal here must not block saving.
  const anyEnabled = Object.values(s).some(v => v.enabled);
  if (featureEnabled("notify_webpush") && "Notification" in window) {
    if (anyEnabled) {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        toast("Browser notifications are blocked — other channels still work");
      } else {
        const sub = await _subscribePush();
        if (!sub) toast("Push subscription failed — browser notifications may not arrive when the app is closed");
      }
    } else {
      await _unsubscribePush();
    }
  }
  if (!await _saveAllChannelFields()) return;
  const r = await fetch("/api/notification-settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(s),
  });
  if (r.ok) {
    toast("Notification settings saved");
    _backToSettingsMenu();
  } else {
    toast("Failed to save notification settings");
  }
});

async function _testBrowserPush() {
  const perm = await Notification.requestPermission();
  if (perm !== "granted") { toast("Allow notifications in browser settings first"); return; }
  const sub = await _subscribePush();
  if (!sub) { toast("Could not register push subscription"); return; }
  const r = await fetch("/api/push-test", { method: "POST" });
  if (r.ok) {
    toast("Test notification sent — check your phone");
  } else {
    const d = await r.json().catch(() => ({}));
    toast(d.error || "Failed to send test notification");
  }
}

// ─── Notification channels (where notifications are sent) ────────────────────
// Web push is built in; the rest are configured here with the same field
// store as Settings → Integrations (/api/integrations), and switched on/off
// with the same feature switches the server enforces (/api/features).
const _NOTIF_CHANNELS = [
  { key: "webpush",  feature: "notify_webpush",  title: "Browser push", prefix: null,
    help: "Notifications on this device, even when the app is closed. Allow notifications in your browser when asked." },
  { key: "ntfy",     feature: "notify_ntfy",     title: "ntfy",     prefix: "ntfy.",
    help: "ntfy.sh or your own ntfy server. Install the ntfy app and subscribe to the topic." },
  { key: "telegram", feature: "notify_telegram", title: "Telegram", prefix: "telegram.",
    help: "Create a bot with @BotFather, send it a message, and enter the bot token and your chat ID." },
  { key: "discord",  feature: "notify_discord",  title: "Discord",  prefix: "discord.",
    help: "Channel settings → Integrations → Webhooks → copy the webhook address." },
  { key: "webhook",  feature: "notify_webhook",  title: "Webhook",  prefix: "webhook.",
    help: "Spooler sends each notification as JSON (POST) to this address." },
];

function _channelFieldsHtml(ch) {
  if (!ch.prefix) return "";
  return integrations.fields.filter(f => f.key.startsWith(ch.prefix)).map(f => {
    const secret = f.type === "secret";
    const placeholder = secret ? (f.set ? "•••• (leave blank to keep)" : "Not set") : "";
    const remove = (secret && f.set && !f.locked)
      ? `<button type="button" class="btn btn-secondary btn-sm" data-ch-clear="${escAttr(f.key)}">Remove</button>` : "";
    return `<div class="integration-field">
      <label>${escHtml(f.label)}
        <input type="${secret ? "password" : "text"}" data-ch-key="${escAttr(f.key)}"
               value="${secret ? "" : escAttr(f.value ?? "")}" placeholder="${escAttr(placeholder)}"
               ${f.locked ? "disabled" : ""} autocomplete="off" />
      </label>${remove ? `<div class="integration-field-meta">${remove}</div>` : ""}
    </div>`;
  }).join("");
}

function _renderNotifChannels() {
  const host = document.getElementById("notif-channels");
  if (!host) return;
  const summary = document.getElementById("notif-channels-summary");
  if (summary) {
    const on = _NOTIF_CHANNELS.filter(ch => features[ch.feature]?.enabled && !features[ch.feature]?.missing).length;
    summary.textContent = on ? `${on} on` : "none on";
  }
  host.innerHTML = _NOTIF_CHANNELS.map(ch => {
    const f = features[ch.feature];
    if (!f) return "";
    const result = integrations.tests?.[ch.key];
    const status = f.missing ? "Not set up" : (f.enabled ? "On" : "Off");
    return `
    <details class="notif-channel" data-channel="${ch.key}">
      <summary>
        <span class="notif-label">${escHtml(ch.title)}</span>
        <span class="notif-channel-status${f.missing ? " missing" : ""}">${status}</span>
        <label class="toggle-switch" onclick="event.stopPropagation()">
          <input type="checkbox" data-ch-feature="${ch.feature}" ${f.enabled ? "checked" : ""} ${f.locked ? "disabled" : ""} />
          <span class="toggle-slider"></span>
        </label>
      </summary>
      <div class="notif-channel-body">
        <p class="report-desc">${escHtml(ch.help)}</p>
        ${_channelFieldsHtml(ch)}
        <div class="integration-test-row">
          <button type="button" class="btn btn-secondary btn-sm" data-ch-test="${ch.key}">Send test</button>
          ${result ? `<span class="integration-test-result ${result.ok ? "ok" : "fail"}">${escHtml(result.message)}</span>` : ""}
        </div>
      </div>
    </details>`;
  }).join("");

  host.querySelectorAll("input[data-ch-feature]").forEach(input => {
    input.addEventListener("change", async () => {
      const key = input.dataset.chFeature, enabled = input.checked;
      try {
        const r = await fetch("/api/features", { method: "PATCH", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ key, enabled }) });
        const data = await r.json();
        if (!r.ok) { toast(data.error || "Could not update", true); input.checked = !enabled; return; }
        features = {}; data.features.forEach(f => { features[f.key] = f; });
        _renderNotifChannels();
      } catch (e) { toast("Could not update: " + e.message, true); input.checked = !enabled; }
    });
  });
  host.querySelectorAll("button[data-ch-clear]").forEach(btn => btn.addEventListener("click", async () => {
    const r = await fetch("/api/integrations", { method: "PATCH", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ clear: [btn.dataset.chClear] }) });
    if (r.ok) { await _reloadChannels(true); toast("Removed"); } else toast("Could not remove", true);
  }));
  host.querySelectorAll("button[data-ch-test]").forEach(btn => btn.addEventListener("click", () => _testChannel(btn.dataset.chTest, btn)));
}

async function _reloadChannels(keepOpen) {
  const open = new Set([...document.querySelectorAll("#notif-channels details[open]")].map(d => d.dataset.channel));
  const r = await fetch("/api/features");
  if (r.ok) { features = {}; (await r.json()).forEach(f => { features[f.key] = f; }); }
  await loadIntegrations();
  _renderNotifChannels();
  if (keepOpen) open.forEach(k => document.querySelector(`#notif-channels details[data-channel="${k}"]`)?.setAttribute("open", ""));
}

// Save the typed-in fields of one channel (blank secrets mean "unchanged").
async function _saveChannelFields(chKey) {
  const values = {};
  document.querySelectorAll(`#notif-channels details[data-channel="${chKey}"] [data-ch-key]`).forEach(i => {
    values[i.dataset.chKey] = i.value;
  });
  if (!Object.keys(values).length) return true;
  const r = await fetch("/api/integrations", { method: "PATCH", headers: { "Content-Type": "application/json" },
                                                body: JSON.stringify({ values }) });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    toast(d.error || "Could not save", true);
    return false;
  }
  return true;
}

async function _testChannel(chKey, btn) {
  if (chKey === "webpush") { await _testBrowserPush(); return; }
  btn.disabled = true; btn.textContent = "Sending…";
  try {
    if (!await _saveChannelFields(chKey)) return;
    const r = await fetch(`/api/integrations/${encodeURIComponent(chKey)}/test`, { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (r.status === 403) toast("Switch this channel on first", true);
    else toast(d.message || (r.ok ? "Sent" : "Failed"), !d.ok);
  } finally {
    await _reloadChannels(true);
  }
}

// Saving the notification page also saves any channel fields typed into it.
async function _saveAllChannelFields() {
  for (const ch of _NOTIF_CHANNELS) {
    if (ch.prefix && !await _saveChannelFields(ch.key)) return false;
  }
  return true;
}

// ─── Printers panel ────────────────────────────────────────────────────────────
const printersPanel = document.getElementById("panel-printers");

const inputType       = document.getElementById("input-type");
const labelAccessCode = document.getElementById("label-access-code");
const inputAccessCode = document.getElementById("input-access-code");

let _editingPrinterId = null;


function resetPrinterForm() {
  _editingPrinterId = null;
  document.getElementById("input-ip").value = "";
  document.getElementById("input-name").value = "";
  inputType.value = "cc1";
  inputType.style.display = "";
  document.getElementById("input-type-readonly").style.display = "none";
  inputAccessCode.value = "";
  inputAccessCode.placeholder = "12345";
  labelAccessCode.style.display = "none";

  document.getElementById("printer-panel-title").textContent = "Add Printer";
  document.getElementById("printer-discover-section").style.display = "";
  document.getElementById("printer-panel-divider").style.display = "";
  document.getElementById("printer-form-label").textContent = "Add manually";
  document.getElementById("btn-modal-confirm").innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M12 5v14M5 12h14"/>
  </svg> Add Printer`;
  document.getElementById("btn-remove-printer").style.display = "none";
}

inputType.addEventListener("change", () => {
  const needsKey = inputType.value === "cc2" || inputType.value === "prusa" || inputType.value === "moonraker";
  labelAccessCode.style.display = needsKey ? "flex" : "none";
  document.getElementById("label-access-code-text").textContent =
    inputType.value === "cc2" ? "MQTT Password" : "API Key";
  document.getElementById("input-access-code").placeholder =
    inputType.value === "moonraker" ? "API key (optional)"
    : inputType.value === "prusa" ? "API key from PrusaLink settings" : "12345";
});

const openPrinters = () => {
  printersPanel.classList.add("open");
  historyBackdrop.classList.add("open");
};
const closePrinters = () => {
  printersPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  resetPrinterForm();
};

document.getElementById("btn-add")?.addEventListener("click", () => {
  resetPrinterForm();
  openPrinters();
});
document.getElementById("btn-printers-close").addEventListener("click", closePrinters);

function _triggerDiscover(btn) {
  send({ action: "discover" });
  btn.disabled = true;
  btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
  </svg> Scanning…`;
  setTimeout(() => {
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
    </svg> Scan for Printers`;
  }, 7000);
}
document.getElementById("btn-discover")?.addEventListener("click", e => {
  openPrinters();
  _triggerDiscover(e.currentTarget);
});

document.getElementById("btn-discover-panel")?.addEventListener("click", e => _triggerDiscover(e.currentTarget));

document.getElementById("btn-modal-confirm").addEventListener("click", () => {
  const ip          = document.getElementById("input-ip").value.trim();
  const name        = document.getElementById("input-name").value.trim();
  const access_code = inputAccessCode.value.trim();
  if (!ip) { toast("Enter an IP address", true); return; }
  if (_editingPrinterId) {
    if (!name) { toast("Enter a printer name", true); return; }
    send({ action: "update_printer", printer_id: _editingPrinterId, name, ip, access_code });
  } else {
    send({ action: "add_printer", ip, name: name || undefined, printer_type: inputType.value, access_code });
  }
  closePrinters();
});

document.getElementById("btn-remove-printer").addEventListener("click", () => {
  if (!_editingPrinterId) return;
  if (confirm("Remove this printer?")) {
    send({ action: "remove_printer", printer_id: _editingPrinterId });
    closePrinters();
  }
});

