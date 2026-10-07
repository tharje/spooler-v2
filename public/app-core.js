/* Spooler frontend: state, theme, WebSocket, message handling, status helpers.
   Split across app-*.js (plain scripts sharing one global scope); load order is in index.html. */
"use strict";

// The backend also accepts WebSocket upgrades on the plain HTTP port itself
// ("combo" mode), so by default we just reconnect to the page's own origin
// (same hostname + port) — this is what makes Spooler work through a
// single-port tunnel/reverse proxy (e.g. Cloudflare Tunnel → http://ip:8080)
// with zero extra config: Cloudflare always presents the browser with the
// standard port (443, so location.port is blank) and forwards the upgrade
// through the same route as regular HTTP.
//
// The one case that still needs a separate port is a *direct* HTTPS
// connection to Spooler's own self-signed listener (HTTPS_PORT, e.g. LAN PWA
// installs) — TLS has to be terminated before the request line is even
// visible, so that path can't be combo'd and keeps using WSS_PORT.
//
// SPOOLER_WS_HOST (WS_HOST env var) forces a specific host for edge cases
// (e.g. running HTTPS_PORT=443 directly with no reverse proxy in front).
const _directHttpsPort = location.protocol === "https:" && location.port && location.port !== "443";
let WS_URL;
if (window.SPOOLER_WS_HOST) {
  WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + window.SPOOLER_WS_HOST;
} else if (_directHttpsPort) {
  WS_URL = `wss://${location.hostname}:${window.SPOOLER_WSS_PORT ?? 8766}`;
} else {
  WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
}
const SPOOLMAN_URL = "/api/spoolman/api/v1";

// ─── Theme ────────────────────────────────────────────────────────────────────
(function () {
  if (localStorage.getItem("theme") === "light") {
    document.body.classList.add("light-mode");
  }
})();

// Redirect to login on any 401 from our own API
const _fetch = window.fetch.bind(window);
window.fetch = async function(url, options) {
  const resp = await _fetch(url, options);
  if (resp.status === 401 && typeof url === "string" && url.startsWith("/api/")) {
    location.replace("/login");
  }
  return resp;
};

let ws       = null;
let printers = {}; // id → printer data
let history  = []; // print history log
let spools   = []; // spool inventory from Spoolman
let trayMap  = {}; // printer_id → { tray_id_str → spoolman_spool_id }
let _prevActiveTray = {}; // printer_id → last seen active_tray_id
let features = {}; // key → {name, description, enabled, locked, missing, risky, requires}

function featureEnabled(key) {
  // Unknown/not-yet-loaded features default to on so the UI doesn't flash
  // hidden-then-shown while /api/features is still loading on first paint.
  return features[key] ? features[key].enabled : true;
}

// ─── Spoolman field helpers ────────────────────────────────────────────────────
function spoolName(s)       { return [s.filament?.vendor?.name, s.filament?.material, s.filament?.name].filter(Boolean).join(" ") || `Spool ${s.id}`; }
function spoolColorHex(s)   { const h = s.filament?.color_hex || "888888"; return h.startsWith("#") ? h : "#" + h; }
function spoolRemaining(s)  { return Math.round(s.remaining_weight ?? 0); }
function spoolTotal(s)      { return Math.round(s.initial_weight ?? 1000); }
function spoolPct(s)        { const t = spoolTotal(s); return t > 0 ? Math.round(spoolRemaining(s) / t * 100) : 0; }
function spoolAssignedTo(s) { return s.location || null; }
// Find a printer by its Spoolman location string (name or legacy ID/IP).
function printerByLocation(loc) {
  if (!loc) return null;
  return printers[loc] || Object.values(printers).find(p => p.name === loc) || null;
}
// The location string to store in Spoolman for a given printer ID — the printer's name.
function printerLocation(printerId) {
  return printers[printerId]?.name || printerId;
}

let currentPickerPrinterId = null;
let currentPickerTrayId    = null; // non-null → picker is in tray-link mode
let _currentFilePrinterId  = null;
let _currentFileList       = []; // cached file objects for the open file browser
let _materialDensityMap = {}; // material name → density g/cm³

// ─── Filament metadata (brands + materials from SpoolmanDB) ───────────────────
async function loadFilamentMeta() {
  try {
    const r = await fetch("/api/filament-meta");
    if (!r.ok) return;
    const { brands, materials } = await r.json();

    _materialDensityMap = {};
    materials.forEach(m => { _materialDensityMap[m.name] = m.density; });

    const bSel = document.getElementById("spool-input-brand");
    const mSel = document.getElementById("spool-input-material");

    // Rebuild brand dropdown
    bSel.innerHTML = '<option value="">– Select brand –</option>';
    brands.forEach(b => {
      const o = document.createElement("option");
      o.value = o.textContent = b;
      bSel.appendChild(o);
    });
    const bOther = document.createElement("option");
    bOther.value = "__other__"; bOther.textContent = "Other…";
    bSel.appendChild(bOther);

    // Rebuild material dropdown
    mSel.innerHTML = '<option value="">– Select material –</option>';
    materials.forEach(m => {
      const o = document.createElement("option");
      o.value = o.textContent = m.name;
      mSel.appendChild(o);
    });
    const mOther = document.createElement("option");
    mOther.value = "__other__"; mOther.textContent = "Other…";
    mSel.appendChild(mOther);
  } catch (_) {}
}

// ─── WebSocket connection ──────────────────────────────────────────────────────
// Exponential backoff instead of a fixed delay — reconnecting instantly in a
// tight loop against a server that's actually down just adds load for no
// benefit; reconnecting fast is far more useful in the common case (a brief
// network blip), so the delay starts short and only grows if it keeps failing.
const RECONNECT_DELAYS = [1000, 2000, 5000, 10000, 30000];
let _reconnectAttempt = 0;
let _reconnectTimer = null;

function _showConnBanner() {
  document.getElementById("conn-banner").hidden = false;
  document.body.classList.add("ws-disconnected");
}
function _hideConnBanner() {
  document.getElementById("conn-banner").hidden = true;
  document.body.classList.remove("ws-disconnected");
}

function connect() {
  clearTimeout(_reconnectTimer);
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log("[WS] Connected");
    _reconnectAttempt = 0;
    _hideConnBanner();
    // Always re-fetch full state on (re)connect, not just the initial load —
    // whatever happened while disconnected (printer events we never saw)
    // must not linger as stale data once the connection is back.
    send({ action: "list_printers" });
    loadHistory();
    loadLedger();
    loadSecurityStatus();
    fetchSpools();
  };

  ws.onmessage = (ev) => {
    try {
      handleMessage(JSON.parse(ev.data));
    } catch (e) {
      console.error("[WS] Bad message", e);
    }
  };

  ws.onclose = (ev) => {
    if (ev.code === 1008) { location.replace("/login"); return; }
    console.warn("[WS] Disconnected, retrying…");
    _showConnBanner();
    const delay = RECONNECT_DELAYS[Math.min(_reconnectAttempt, RECONNECT_DELAYS.length - 1)];
    _reconnectAttempt++;
    _reconnectTimer = setTimeout(connect, delay);
  };

  ws.onerror = () => ws.close();
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

// Mobile browsers routinely kill WebSockets while a tab/PWA is backgrounded.
// Don't wait for the next scheduled reconnect (which could be up to 30s into
// an already-stale backoff) once the user actually comes back to look at it.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
    _reconnectAttempt = 0;
    clearTimeout(_reconnectTimer);
    connect();
  } else if (ws.readyState === WebSocket.OPEN) {
    send({ action: "list_printers" });
    loadHistory();
    fetchSpools();
  }
});

// ─── Message handling ──────────────────────────────────────────────────────────
function handleMessage(msg) {
  switch (msg.type) {
    case "printer_update":
      _noteServerTime(msg.printer.server_time);
      printers[msg.printer.id] = msg.printer;
      renderPrinter(msg.printer);
      if (msg.printer.id === _currentFilePrinterId) updateUploadUi();
      syncEmptyState();
      _checkActiveTrayChange(msg.printer);
      break;
    case "printer_removed":
      delete printers[msg.printer_id];
      const el = document.getElementById(`card-${CSS.escape(msg.printer_id)}`);
      if (el) el.remove();
      syncEmptyState();
      break;
    case "info":
      toast(msg.message);
      break;
    case "error":
      toast(msg.message, true);
      break;
    case "ledger_update":
      _setLedgerBadge(msg);
      if (spoolsPanel.classList.contains("open")) loadLedger();
      break;
    case "history_update": {
      const h = history.find(e => e.id === msg.id);
      if (h) { Object.assign(h, msg.fields || {}); renderHistory(); }
      break;
    }
    case "history_snapshot": {
      const h = history.find(e => e.id === msg.id);
      if (h) { h.snapshot = true; renderHistory(); }
      break;
    }
    case "history_entry":
      history.unshift(msg.entry);
      renderHistory();
      toast(`Print logged: ${msg.entry.filament_g}g used`);
      break;
    case "spool_empty":
      spools = spools.map(s => s.id === msg.spool.id ? msg.spool : s);
      Object.values(printers).forEach(p => renderPrinter(p));
      toast(`⚠ Spool empty: ${spoolName(msg.spool)} — assign a new spool`, true);
      break;
    case "spool_low":
      spools = spools.map(s => s.id === msg.spool.id ? msg.spool : s);
      Object.values(printers).forEach(p => renderPrinter(p));
      toast(`Spool low: ${spoolName(msg.spool)} — ${spoolRemaining(msg.spool)}g remaining`);
      break;
    case "file_list":
      if (msg.printer_id === _currentFilePrinterId) renderFileList(msg.files, msg.error);
      break;
    case "file_info":
      _applyFileInfo(msg);
      break;
    case "tray_map":
      trayMap = msg.tray_map || {};
      Object.values(printers).forEach(p => renderPrinter(p));
      break;
    case "cc2_discovered":
      for (const ip of (msg.ips || [])) {
        toastAction(`CC2 found at ${ip} — enter access code to add`, "Add", () => {
          resetPrinterForm();
          document.getElementById("input-ip").value = ip;
          document.getElementById("input-name").value = `CC2 (${ip})`;
          inputType.value = "cc2";
          labelAccessCode.style.display = "flex";
          openPrinters();
          inputAccessCode.focus();
        }, 12000);
      }
      break;
    case "features_changed":
      _applyFeatures(msg.features || []);
      break;
  }
}

function _checkActiveTrayChange(printer) {
  const ci = printer.status?.canvas_info;
  if (!ci) return;
  const trayId = ci.active_tray_id ?? -1;
  const prev   = _prevActiveTray[printer.id] ?? -2;
  _prevActiveTray[printer.id] = trayId;
  if (trayId < 0 || trayId === prev) return;
  // Active tray changed — auto-assign the linked spool if one is set
  const linked = (trayMap[printer.id] || {})[String(trayId)];
  if (linked != null) {
    console.log(`[Tray] Active tray ${trayId} → auto-assigning spool ${linked}`);
    assignSpool(printer.id, linked);
  }
}

// ─── Status helpers ────────────────────────────────────────────────────────────
// The backend's PrinterConnection.to_dict() already normalizes every
// protocol's raw status codes into a single small vocabulary (see
// printers/base.py classify_display_state): offline, idle, preparing,
// printing, pausing, paused, complete, cancelled, stopping, error, unknown.
// The dot and badge both read that same "state" string directly — no more
// separate raw-code interpretation here, so they can never disagree with
// each other or with the backend's own notion of what's happening.
const STATE_LABEL = {
  offline:   "Offline",
  idle:      "Idle",
  preparing: "Preparing",
  printing:  "Printing",
  pausing:   "Pausing",
  paused:    "Paused",
  complete:  "Complete",
  cancelled: "Cancelled",
  stopping:  "Stopping",
  error:     "Error",
  unknown:   "Unknown status",
};

function getPrintStatus(printer) {
  return printer.state || (printer.connected ? "idle" : "offline");
}

function statusClass(s) {
  return STATE_LABEL[s] ? s : "idle";
}

function isActivelyPrinting(printer) {
  const s = getPrintStatus(printer);
  return ["printing", "preparing"].includes(s);
}


// firmware-badge:begin
// Firmware that hasn't been tested with this version of Spooler gets a small
// yellow notice (information only; nothing is blocked). An unknown version
// (firmware_tested == null) and a tested one show nothing.
const _FIRMWARE_ISSUES_URL = "https://github.com/tharje/spooler-v2/issues";
function firmwareBadgeHtml(printer) {
  if (printer.firmware_tested !== false || !printer.firmware_version) return "";
  const tip = `Firmware ${printer.firmware_version} has not been tested with this version of Spooler. ` +
              `If something doesn't work as expected, it may be the firmware. Please report it on GitHub.`;
  return ` <a class="fw-badge" href="${_FIRMWARE_ISSUES_URL}" target="_blank" rel="noopener" ` +
         `title="${escAttr(tip)}" aria-label="${escAttr(tip)}">⚠ untested firmware</a>`;
}
// firmware-badge:end

// clock-sync:begin
// last_seen is the SERVER's epoch time. The browser's clock may differ, so every
// printer message carries server_time and we keep the difference as an offset;
// "now" for any comparison with last_seen is then the server's now.
let _clockOffsetS = 0;
function _noteServerTime(serverEpochS) {
  if (typeof serverEpochS === "number" && isFinite(serverEpochS)) _clockOffsetS = serverEpochS - Date.now() / 1000;
}
function _serverNowS() { return Date.now() / 1000 + _clockOffsetS; }

// Data goes stale faster while actively printing (30s) than otherwise (2min)
// -- a frozen temperature reading matters a lot more mid-print than while idle.
function isStale(printer) {
  if (!printer.connected || printer.last_seen == null) return false; // offline is its own distinct state
  const thresholdS = isActivelyPrinting(printer) ? 30 : 120;
  return (_serverNowS() - printer.last_seen) > thresholdS;
}

function formatAgo(epochSeconds) {
  const diff = Math.max(0, Math.round(_serverNowS() - epochSeconds));
  if (diff < 60) return `${diff}s ago`;
  const mins = Math.round(diff / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}
// clock-sync:end

// Returns the printer object that currently has this spool loaded as its active tray, or null.
function getSpoolActivePrinter(spoolId) {
  for (const [pid, trays] of Object.entries(trayMap)) {
    const p = printers[pid];
    if (!p) continue;
    const activeTray = p.status?.canvas_info?.active_tray_id ?? -1;
    if (activeTray >= 0 && trays[String(activeTray)] === spoolId) return p;
  }
  return null;
}

function isPaused(printer) {
  const s = getPrintStatus(printer);
  return ["paused", "pausing"].includes(s);
}

// Minimal reason box — full visual treatment (icons per category, etc.) is T3's
// job; this just satisfies T2's "show why" requirement without guessing text
// for anything not actually verified.
const _REASON_KIND_LABEL = { pause: "Paused", stop: "Stopped", error: "Error" };
const _REASON_CATEGORY_LABEL = {
  filament_runout: "Filament runout",
  nozzle_clog:     "Nozzle clog",
  thermal:         "Thermal issue",
  collision:       "Collision detected",
  power_loss:      "Power loss",
  door_open:       "Door open",
  leveling:        "Leveling failed",
  fan:             "Fan fault",
  motion:          "Motion fault",
  sensor:          "Sensor fault",
  hardware:        "Hardware fault",
  connection:      "Lost communication with a printer part",
  system:          "System error",
  filament_feed:   "Filament feed problem",
  filament_tangle: "Filament tangled",
  user:            "User action",
  unknown:         "Reason not yet identified",
};
const _REASON_INITIATOR_LABEL = {
  spooler: "from Spooler",
  printer: "reported by printer",
  unknown: "from printer screen, app, or unknown source",
};

function renderReasonBox(printer) {
  const r = printer.state_reason;
  if (!r) return "";
  const kindLabel = _REASON_KIND_LABEL[r.kind] || "Notice";
  const initiator = _REASON_INITIATOR_LABEL[r.initiated_by] || _REASON_INITIATOR_LABEL.unknown;
  // A spooler-initiated pause/stop already has a known, obvious cause (someone
  // clicked the button) — showing a generic "reason not yet identified" after
  // it would be actively misleading, so only append category/message detail
  // when there's something the printer/protocol actually reported.
  const detail = r.initiated_by === "spooler"
    ? (r.message || "")
    : (r.message || _REASON_CATEGORY_LABEL[r.category] || _REASON_CATEGORY_LABEL.unknown);
  return `
    <div class="reason-box ${r.kind === "error" ? "reason-box-error" : ""}">
      <div class="reason-box-main">
        <strong>${escHtml(kindLabel)}</strong> ${escHtml(initiator)}
        ${detail ? ` — ${escHtml(detail)}` : ""}
      </div>
      ${r.action ? `<div class="reason-box-action">${escHtml(r.action)}</div>` : ""}
      ${r.code ? `<div class="reason-box-code">Code: ${escHtml(String(r.code))}</div>` : ""}
    </div>
  `;
}

function getProgress(printer) {
  const pi = printer.status?.PrintInfo;
  if (!pi) return null;
  if ((pi.TotalLayer ?? 0) > 0)
    return Math.min(100, Math.round((pi.CurrentLayer / pi.TotalLayer) * 100));
  if ((pi.TotalTicks ?? 0) > 0)
    return Math.min(100, Math.round((pi.CurrentTicks / pi.TotalTicks) * 100));
  return null;
}

function getFilename(printer) {
  return printer.status?.PrintInfo?.Filename || printer.status?.PrintInfo?.FileName || "";
}

function formatTime(secs) {
  if (!secs || secs < 0) return "--:--";
  secs = Math.round(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function formatTimeLong(secs) {
  if (!secs || secs < 0) return "--";
  secs = Math.round(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return `${String(h).padStart(2, "0")}h${String(m).padStart(2, "0")}m${String(s).padStart(2, "0")}s`;
}

// Manually formats hours/minutes (never via toLocaleTimeString) so the
// result is always 24-hour regardless of the browser's locale.
function formatFinishAt(remainingSecs) {
  if (!remainingSecs || remainingSecs <= 0) return null;
  const now    = new Date();
  const finish = new Date(now.getTime() + remainingSecs * 1000);
  const time   = `${String(finish.getHours()).padStart(2, "0")}:${String(finish.getMinutes()).padStart(2, "0")}`;
  const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const daysAhead = Math.round((startOfDay(finish) - startOfDay(now)) / 86400000);
  if (daysAhead <= 0) return `Done ~${time}`;
  if (daysAhead === 1) return `Done tomorrow ${time}`;
  const dateStr = finish.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `Done ${dateStr}, ${time}`;
}

function tempColor(t) {
  if (!t) return "";
  if (t > 150) return "hot";
  if (t > 50)  return "warm";
  return "cool";
}

