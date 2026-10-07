/* Spooler – frontend app.js */
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

// ─── Rendering ─────────────────────────────────────────────────────────────────
function renderPrinter(printer) {
  const grid = document.getElementById("printer-grid");
  const cardId = `card-${printer.id}`;
  let card = document.getElementById(cardId);

  if (!card) {
    card = document.createElement("div");
    card.className = "printer-card";
    card.id = cardId;
    grid.appendChild(card);
  }

  const status   = getPrintStatus(printer);
  const sc       = statusClass(status);
  const progress = getProgress(printer);
  const filename = getFilename(printer);
  const printing = isActivelyPrinting(printer);
  const paused   = isPaused(printer);
  const connected = printer.connected;
  const stale     = isStale(printer);
  card.classList.toggle("card-stale", stale);

  const nozzle     = printer.status?.TempOfNozzle    ?? printer.status?.NozzleTemp    ?? 0;
  const nozzleTgt  = printer.status?.TempTargetNozzle?? printer.status?.NozzleTempTarget ?? 0;
  const bed        = printer.status?.TempOfHotbed     ?? printer.status?.BedTemp       ?? 0;
  const bedTgt     = printer.status?.TempTargetHotbed ?? printer.status?.BedTempTarget ?? 0;
  const chamber    = printer.status?.TempOfBox        ?? printer.status?.ChamberTemp   ?? 0;
  const chamberTgt = printer.status?.TempTargetBox    ?? 0;

  const curLayer   = printer.status?.PrintInfo?.CurrentLayer ?? 0;
  const totalLayer = printer.status?.PrintInfo?.TotalLayer   ?? 0;
  const layerLabel = totalLayer > 0 ? `Layer ${curLayer} / ${totalLayer}` : (curLayer > 0 ? `Layer ${curLayer}` : "");
  const elapsed   = printer.status?.PrintInfo?.PrintTime  ?? 0;
  const remaining = printer.status?.PrintInfo?.RemainTime ?? 0;
  const filamentMm = printer.filament_mm ?? 0;
  const filamentG  = printer.filament_g  ?? 0;
  const lightOn    = getLightOn(printer);

  const cameraUrl = printer.connected && printer.camera_url && featureEnabled("camera")
                     && printer.camera_connected !== false
    ? `/api/camera/${encodeURIComponent(printer.id)}`
    : null;

  // Preserve the existing camera img element so its MJPEG stream connection
  // survives innerHTML replacement (every printer_update would kill it otherwise)
  const prevCameraImg = card.querySelector('.card-camera img');

  const stateLabel = STATE_LABEL[sc] || STATE_LABEL.unknown;
  const badgeText  = (sc === "error" && printer.state_reason?.code)
    ? `${status} (${printer.state_reason.code})`
    : (printer.phase && ["preparing", "error", "printing"].includes(sc) ? printer.phase : status);

  card.innerHTML = `
    <!-- Header -->
    <div class="card-header">
      <div class="status-dot ${sc}" role="img" aria-label="${escAttr(stateLabel)}" title="${escAttr(stateLabel)}"></div>
      <div class="card-header-info">
        <div class="card-title">${escHtml(printer.name)}</div>
        <div class="card-subtitle">${escHtml(printer.ip)}${printer.firmware_version ? ` · fw ${escHtml(printer.firmware_version)}` : ""}${firmwareBadgeHtml(printer)}</div>
      </div>
      <span class="status-badge ${sc}">${escHtml(badgeText)}</span>
      <button class="card-files-btn" onclick="openFileBrowser('${escAttr(printer.id)}')" title="Browse files">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
        </svg>
      </button>
    </div>

    ${renderReasonBox(printer)}
    ${stale ? `<div class="stale-notice">Last updated ${formatAgo(printer.last_seen)}</div>` : ""}

    <!-- Camera -->
    <div class="card-camera">
      <div class="camera-placeholder" style="display:flex">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
          <path d="M23 7l-7 5 7 5V7z"/>
          <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
        </svg>
        <span>${!connected ? "Printer offline" : printer.camera_connected === false ? "Camera not connected on printer" : "No camera feed"}</span>
      </div>
    </div>

    <!-- Progress (only when printing/paused) -->
    ${(printing || paused) ? `
    <div class="card-progress-wrap">
      <div class="progress-header">
        <span class="progress-filename" title="${escAttr(filename)}">${escHtml(filename || "Unknown file")}</span>
        <span class="progress-pct">${progress ?? 0}%</span>
      </div>
      <div class="progress-bar-bg">
        <div class="progress-bar-fill" style="width:${progress ?? 0}%"></div>
      </div>
      <div class="progress-info">
        <span>Elapsed: ${formatTime(elapsed)}</span>
        ${layerLabel ? `<span class="progress-layer">${escHtml(layerLabel)}</span>` : ""}
        <span>Remaining: ${formatTime(remaining)}</span>
      </div>
      ${paused
        ? `<div class="finish-time">Paused — finish time updates when print resumes</div>`
        : (() => {
            const finishLabel = formatFinishAt(remaining);
            return finishLabel ? `<div class="finish-time">${escHtml(finishLabel)}</div>` : "";
          })()
      }
      ${filamentMm > 0 ? `
      <div class="filament-info">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/>
        </svg>
        <span>${(filamentMm / 1000).toFixed(2)} m &nbsp;·&nbsp; ~${filamentG} g</span>
      </div>` : ""}
    </div>
    ` : ""}

    <!-- Spool (hidden for canvas printers — tray linking handles assignment) -->
    ${!printer.status?.canvas_info?.canvas_list?.length ? (() => {
      const s = spools.find(sp => printerByLocation(spoolAssignedTo(sp))?.id === printer.id);
      const pct = s ? spoolPct(s) : 0;
      const isEmpty = s && spoolRemaining(s) === 0;
      const isLow   = s && !isEmpty && spoolTotal(s) > 0 && pct < 10;
      const barColor = isEmpty ? "var(--red)" : isLow ? "var(--yellow)" : null;
      return `<div class="card-spool${isEmpty ? " spool-empty" : isLow ? " spool-low" : ""}">
        ${s ? `
          <div class="spool-dot" style="background:${escAttr(spoolColorHex(s))}"></div>
          <div class="spool-card-info">
            <div class="spool-card-name">${escHtml(spoolName(s))}${isEmpty ? ' <span class="spool-tag empty">Empty</span>' : isLow ? ' <span class="spool-tag low">Low</span>' : ""}</div>
            <div class="spool-bar-wrap">
              <div class="spool-bar-fill" style="width:${pct}%${barColor ? ";background:" + barColor : ""}"></div>
            </div>
            <div class="spool-card-remaining">${spoolRemaining(s)}g / ${spoolTotal(s)}g</div>
          </div>
        ` : `<span class="spool-none">No spool assigned</span>`}
        <button class="btn btn-sm btn-secondary spool-assign-btn"
                onclick="openSpoolPicker('${escAttr(printer.id)}')">
          ${s ? "Change" : "Assign"}
        </button>
      </div>`;
    })() : ""}

    <!-- Canvas / multi-material trays (CC2 with canvas unit) -->
    ${(() => {
      const ci = printer.status?.canvas_info;
      if (!ci || !ci.canvas_list?.length) return "";
      const activeTrayId = ci.active_tray_id ?? -1;
      const trays = ci.canvas_list.flatMap(c => c.tray_list || []);
      if (!trays.length) return "";
      const pid = printer.id;
      const trayHtml = trays.map(t => {
        const color      = t.filament_color || "#888888";
        const active     = t.tray_id === activeTrayId;
        const label      = t.filament_type || "?";
        const fullName   = [t.brand, t.filament_name].filter(Boolean).join(" ");
        const linkedId   = (trayMap[pid] || {})[String(t.tray_id)];
        const linkedSpool = linkedId != null ? spools.find(s => s.id === linkedId) : null;
        const linkedChip = linkedSpool
          ? `<div class="canvas-tray-spool" title="${escAttr(spoolName(linkedSpool))}">
               <div class="canvas-tray-spool-dot" style="background:${escAttr(spoolColorHex(linkedSpool))}"></div>
               <span>${escHtml(spoolName(linkedSpool))}</span>
             </div>`
          : `<div class="canvas-tray-spool canvas-tray-spool-empty">No spool</div>`;
        return `<div class="canvas-tray${active ? " canvas-tray-active" : ""}"
                     title="${escAttr(fullName || label)}"
                     onclick="openTrayPicker('${escAttr(pid)}', ${t.tray_id})">
          <div class="canvas-tray-num">${t.tray_id + 1}</div>
          <div class="canvas-tray-dot" style="background:${escAttr(color)}"></div>
          <div class="canvas-tray-label">${escHtml(label)}</div>
          ${linkedChip}
        </div>`;
      }).join("");
      return `<div class="card-canvas">
        <div class="canvas-header">Canvas <span class="canvas-tray-count">${trays.length} slots</span></div>
        <div class="canvas-trays">${trayHtml}</div>
      </div>`;
    })()}

    <!-- Temperatures -->
    <div class="card-temps">
      <div class="temp-block">
        <div class="temp-label">Nozzle</div>
        <div class="temp-value ${tempColor(nozzle)}">${nozzle ? nozzle.toFixed(0) : "--"}<span style="font-size:12px;font-weight:400">°C</span></div>
        <div class="temp-target">Target: ${nozzleTgt ? nozzleTgt.toFixed(0) + "°C" : "--"}</div>
      </div>
      <div class="temp-block">
        <div class="temp-label">Bed</div>
        <div class="temp-value ${tempColor(bed)}">${bed ? bed.toFixed(0) : "--"}<span style="font-size:12px;font-weight:400">°C</span></div>
        <div class="temp-target">Target: ${bedTgt ? bedTgt.toFixed(0) + "°C" : "--"}</div>
      </div>
      <div class="temp-block">
        <div class="temp-label">Chamber</div>
        <div class="temp-value ${tempColor(chamber)}">${chamber ? chamber.toFixed(0) : "--"}<span style="font-size:12px;font-weight:400">°C</span></div>
        <div class="temp-target">Target: ${chamberTgt ? chamberTgt.toFixed(0) + "°C" : "--"}</div>
      </div>
    </div>

    <!-- Controls -->
    <div class="card-controls">
      ${printing ? `
        <button class="btn btn-secondary btn-sm" onclick="printerAction('${escAttr(printer.id)}','pause')">
          <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
          Pause
        </button>
        <button class="btn btn-danger btn-sm" onclick="confirmStop('${escAttr(printer.id)}')">
          <svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
          Stop
        </button>
      ` : ""}
      ${paused ? `
        <button class="btn btn-primary btn-sm" onclick="printerAction('${escAttr(printer.id)}','resume')">
          <svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>
          Resume
        </button>
        <button class="btn btn-danger btn-sm" onclick="confirmStop('${escAttr(printer.id)}')">
          <svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
          Stop
        </button>
      ` : ""}
      <div class="controls-spacer"></div>
      <button class="btn btn-sm btn-secondary${lightOn ? " btn-light-on" : ""}"
              onclick="printerAction('${escAttr(printer.id)}','${lightOn ? "light_off" : "light_on"}')"
              title="${lightOn ? "Light on" : "Light off"}"
              aria-label="${lightOn ? "Light on" : "Light off"}"
              aria-pressed="${lightOn}"
              ${!connected ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="5"/>
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
        </svg>
      </button>
      ${(printing || paused) ? (() => {
        const sf = printer.printer_type === "cc2"
          ? (printer.status?.SpeedFactor ?? 100)
          : (printer.status?.PrintInfo?.PrintSpeedPct ?? 100);
        const sid = `speed-val-${printer.id}`;
        return `<div class="card-speed">
          <span class="speed-label">Speed: <span id="${escAttr(sid)}">${sf}%</span></span>
          <input type="range" class="speed-slider" min="10" max="200" step="5" value="${sf}"
                 oninput="document.getElementById('${escAttr(sid)}').textContent=this.value+'%'"
                 onchange="setSpeed('${escAttr(printer.id)}',+this.value)">
        </div>`;
      })() : ""}
    </div>
  `;

  const cameraDiv = card.querySelector('.card-camera');
  const placeholder = cameraDiv.querySelector('.camera-placeholder');
  if (cameraUrl) {
    if (prevCameraImg) {
      // Re-attach preserved stream; clear src first if printer just came back online
      // to force reconnect (avoids stale broken stream from a prior disconnect)
      if (!prevCameraImg._connected && connected) {
        prevCameraImg.src = cameraUrl;
      }
      prevCameraImg._connected = connected;
      prevCameraImg._baseUrl = cameraUrl;
      cameraDiv.insertBefore(prevCameraImg, cameraDiv.firstChild);
      placeholder.style.display = 'none';
      prevCameraImg.style.display = '';
    } else {
      const img = document.createElement('img');
      img.src = cameraUrl;
      img.alt = 'Camera feed';
      img._connected = connected;
      img._baseUrl = cameraUrl;
      // Chrome/Chromium does not reliably re-fire "load" for each part of a
      // multipart/x-mixed-replace MJPEG stream (only Firefox does), so load
      // timing can't be used as a per-frame staleness signal across browsers
      // -- it caused "Camera image not updated" to fire constantly on Chrome
      // even while the stream was actively working. The "error" event still
      // fires correctly when the stream genuinely drops, so that's all we
      // rely on here.
      img.addEventListener('load',  () => { placeholder.style.display = 'none'; });
      img.addEventListener('error', () => { img.style.display = 'none'; placeholder.style.display = 'flex'; });
      cameraDiv.insertBefore(img, cameraDiv.firstChild);
    }
  }

  // Only CC2 currently reports LightStatus -- printers that never report it
  // (CC1) must not show this, since getLightOn() falling back to "off" for
  // them would make the overlay permanently cover a working camera feed.
  const lightKnown = printer.status?.LightStatus != null;
  let lightOverlay = cameraDiv.querySelector('.camera-light-overlay');
  if (cameraUrl && connected && lightKnown && !lightOn) {
    if (!lightOverlay) {
      lightOverlay = document.createElement('div');
      lightOverlay.className = 'camera-light-overlay';
      lightOverlay.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="5"/>
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
        </svg>
        <span>Light off</span>
      `;
      lightOverlay.addEventListener('click', () => printerAction(printer.id, 'light_on'));
      cameraDiv.appendChild(lightOverlay);
    }
  } else if (lightOverlay) {
    lightOverlay.remove();
  }
}

function syncEmptyState() {
  const empty = document.getElementById("empty-state");
  const grid  = document.getElementById("printer-grid");
  const has   = Object.keys(printers).length > 0;
  empty.style.display = has ? "none" : "";
  grid.style.display  = has ? ""     : "none";
}

// ─── User actions ──────────────────────────────────────────────────────────────
const _lightPending = {}; // printerId → { on: bool, until: ms timestamp }

function getLightOn(printer) {
  const p = _lightPending[printer.id];
  if (p && Date.now() < p.until) return p.on;
  return !!printer.status?.LightStatus?.SecondLight;
}

function printerAction(id, action) {
  if (action === "light_on" || action === "light_off") {
    _lightPending[id] = { on: action === "light_on", until: Date.now() + 3000 };
    if (printers[id]) renderPrinter(printers[id]);
  }
  send({ action, printer_id: id });
}

function setSpeed(id, speed) {
  send({ action: "set_speed", printer_id: id, speed });
}

function confirmStop(id) {
  if (confirm("Stop the current print? This cannot be undone.")) {
    send({ action: "stop", printer_id: id });
  }
}

function removePrinter(id) {
  if (confirm("Remove this printer?")) {
    send({ action: "remove_printer", printer_id: id });
  }
}

function openPrinterSettings(id) {
  const p = printers[id];
  if (!p) return;
  _editingPrinterId = id;

  document.getElementById("input-name").value = p.name || "";
  document.getElementById("input-ip").value = p.ip || "";
  inputType.value = p.printer_type || "cc1";
  inputType.style.display = "none";
  document.getElementById("input-type-readonly").style.display = "";
  const typeLabels = {
    cc1:       "CC1 – Centauri Carbon 1 (WebSocket/SDCP)",
    cc2:       "CC2 – Centauri Carbon 2 (MQTT)",
    prusa:     "Prusa – PrusaLink HTTP API",
    moonraker: "Klipper – Moonraker HTTP API",
  };
  document.getElementById("input-type-readonly").textContent = typeLabels[p.printer_type] || typeLabels.cc1;
  const needsKey = p.printer_type === "cc2" || p.printer_type === "prusa" || p.printer_type === "moonraker";
  const keyLabel = p.printer_type === "cc2" ? "MQTT Password" : "API Key";
  const keyHint  = p.printer_type === "moonraker"
    ? "API key (optional)"
    : p.printer_type === "prusa" ? "API key from PrusaLink settings" : "12345";
  inputAccessCode.value = "";
  inputAccessCode.placeholder = p.has_access_code ? "Leave blank to keep current" : keyHint;
  document.getElementById("label-access-code-text").textContent = keyLabel;
  labelAccessCode.style.display = needsKey ? "flex" : "none";

  document.getElementById("printer-panel-title").textContent = p.name || "Edit Printer";
  document.getElementById("printer-discover-section").style.display = "none";
  document.getElementById("printer-panel-divider").style.display = "none";
  document.getElementById("printer-form-label").textContent = "Printer details";
  document.getElementById("btn-modal-confirm").innerHTML = "Save Changes";
  document.getElementById("btn-remove-printer").style.display = "";

  openPrinters();
  document.getElementById("input-name").focus();
}

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
    layer:           { enabled: gb("notif-layer-on"),           layer:     gv("notif-layer-number") },
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

// ─── File browser ─────────────────────────────────────────────────────────────
function openFileBrowser(printerId) {
  _currentFilePrinterId = printerId;
  const p = printers[printerId];
  document.getElementById("files-modal-title").textContent = `Files – ${p?.name || printerId}`;
  document.getElementById("files-list").innerHTML = "";
  document.getElementById("files-loading").style.display = "";
  document.getElementById("modal-files").classList.add("open");
  updateUploadUi();
  send({ action: "list_files", printer_id: printerId });
}

// ─── File upload ──────────────────────────────────────────────────────────────
let _uploading = false;

function updateUploadUi() {
  const wrap = document.getElementById("files-upload");
  if (!wrap) return;
  const on = featureEnabled("file_upload");
  wrap.style.display = on ? "" : "none";
  if (!on) return;
  const p = printers[_currentFilePrinterId];
  const ok = !!(p && p.supports_upload) && !_uploading;
  document.getElementById("btn-files-upload").disabled = !ok;
  document.getElementById("files-dropzone").classList.toggle("disabled", !ok);
  document.getElementById("files-upload-hint").textContent = p && !p.supports_upload
    ? (p.upload_unsupported_reason || "Upload isn't supported for this printer.")
    : "or drop a .gcode file here";
  const cb = document.getElementById("files-upload-start");
  cb.disabled = !ok || !p || p.state !== "idle";
  if (cb.disabled) cb.checked = false;
}

function uploadFile(file) {
  const printerId = _currentFilePrinterId;
  const p = printers[printerId];
  if (_uploading || !p || !p.supports_upload) return;
  const start = document.getElementById("files-upload-start").checked;
  const prog = document.getElementById("files-upload-progress");
  const bar = document.getElementById("files-upload-bar");
  const status = document.getElementById("files-upload-status");
  const done = (msg, isErr) => {
    _uploading = false;
    status.textContent = msg;
    status.style.color = isErr ? "var(--red)" : "";
    updateUploadUi();
  };
  _uploading = true;
  updateUploadUi();
  prog.style.display = "";
  bar.style.width = "0";
  status.style.color = "";
  status.textContent = "0%";
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload/${encodeURIComponent(printerId)}?name=${encodeURIComponent(file.name)}&start=${start ? 1 : 0}`);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    bar.style.width = pct + "%";
    status.textContent = pct < 100 ? `${pct}%` : "Sending to printer…";
  };
  xhr.onload = () => {
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status === 200 && body.ok) {
      bar.style.width = "100%";
      done(start ? "Uploaded – print started" : "Uploaded", false);
      toast(`Uploaded: ${file.name}`);
      if (_currentFilePrinterId === printerId) send({ action: "list_files", printer_id: printerId });
    } else {
      done(body.error || `Upload failed (HTTP ${xhr.status})`, true);
    }
  };
  xhr.onerror = () => done("Upload failed: network error", true);
  xhr.send(file);
}

(function initUpload() {
  const btn = document.getElementById("btn-files-upload");
  const input = document.getElementById("files-upload-input");
  const zone = document.getElementById("files-dropzone");
  if (!btn || !input || !zone) return;
  btn.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    if (input.files[0]) uploadFile(input.files[0]);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    if (!zone.classList.contains("disabled")) zone.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
  }));
  zone.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (f && !zone.classList.contains("disabled")) uploadFile(f);
  });
})();

function closeFileBrowser() {
  document.getElementById("modal-files").classList.remove("open");
  _currentFilePrinterId = null;
}

function renderFileList(files, error) {
  _currentFileList = files || [];
  document.getElementById("files-loading").style.display = "none";
  const list = document.getElementById("files-list");

  if (error) {
    list.innerHTML = `<p class="files-empty" style="color:var(--red)">${escHtml(error)}</p>`;
    return;
  }
  if (!files || files.length === 0) {
    list.innerHTML = '<p class="files-empty">No files found on this printer.</p>';
    return;
  }

  const ul = document.createElement("ul");
  ul.className = "files-list-items";

  for (const f of files) {
    const li = document.createElement("li");
    li.className = "file-item" + (f.is_dir ? " file-item-dir" : "");

    const nameEl = document.createElement("div");
    nameEl.className = "file-name";
    nameEl.textContent = f.name || f.path || "";
    nameEl.title = f.path || "";
    li.appendChild(nameEl);

    const metaRow = document.createElement("div");
    metaRow.className = "file-meta-row";

    const sizeEl = document.createElement("span");
    sizeEl.className = "file-size";
    sizeEl.textContent = f.is_dir ? "" : formatFileSize(f.size);
    metaRow.appendChild(sizeEl);

    if (!f.is_dir) {
      const filePath = f.path;
      const fileName = f.name || filePath.split("/").pop() || filePath;

      const actions = document.createElement("div");
      actions.className = "file-actions";

      const btn = document.createElement("button");
      btn.className = "btn btn-primary btn-sm";
      btn.textContent = "Print";
      btn.addEventListener("click", () => {
        const meta = _currentFileList.find(x => x.path === filePath) || {};
        openPrintOpts(filePath, fileName, meta);
      });
      actions.appendChild(btn);

      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-danger btn-sm";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", () => {
        if (confirm(`Delete "${fileName}"?`)) {
          send({ action: "delete_file", printer_id: _currentFilePrinterId, filename: filePath });
          toast(`Deleting: ${fileName}`);
          setTimeout(() => {
            document.getElementById("files-list").innerHTML = "";
            document.getElementById("files-loading").style.display = "";
            send({ action: "list_files", printer_id: _currentFilePrinterId });
          }, 600);
        }
      });
      actions.appendChild(delBtn);
      metaRow.appendChild(actions);
    }

    li.appendChild(metaRow);
    ul.appendChild(li);
  }

  list.innerHTML = "";
  list.appendChild(ul);
}

function formatFileSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${(bytes / 1073741824).toFixed(2)} GB`;
}

// ─── Print Options modal (CC1 + CC2) ──────────────────────────────────────────
let _printOptsFile = null;
let _printOptsName = null;

let _printOptsFilename = null; // bare filename used as key for file_info matching

function _applyFileInfo(msg) {
  // Only update if the modal is still open for the same printer+file
  if (!_printOptsFilename || msg.printer_id !== _currentFilePrinterId) return;
  if (msg.filename !== _printOptsFilename) return;
  const modal = document.getElementById("modal-print-opts");
  if (!modal.classList.contains("open")) return;

  // Thumbnail
  if (msg.thumbnail_b64) {
    const thumbImg  = document.getElementById("print-opts-thumb");
    const thumbWrap = document.getElementById("print-opts-thumb-wrap");
    thumbImg.src = `data:image/png;base64,${msg.thumbnail_b64}`;
    thumbImg.style.display = "";
    thumbWrap.style.display = "";
  }

  // Stats (fill in if present and currently showing --)
  const statsEl = document.getElementById("print-opts-stats");
  if (msg.print_time || msg.layers || msg.filament_g) {
    statsEl.style.display = "";
    if (msg.print_time)
      document.getElementById("print-opts-stat-time-val").textContent = formatTimeLong(msg.print_time);
    if (msg.filament_g != null)
      document.getElementById("print-opts-stat-filament-val").textContent =
        `${parseFloat(msg.filament_g).toFixed(2)}g`;
    if (msg.layers != null)
      document.getElementById("print-opts-stat-layers-val").textContent = msg.layers;
  }
}

function openPrintOpts(filePath, fileName, meta = {}) {
  _printOptsFile = filePath;
  _printOptsName = fileName;
  _printOptsFilename = fileName; // used for file_info matching
  const printerType  = printers[_currentFilePrinterId]?.printer_type;
  const isCC2        = printerType === "cc2";
  const isPrusa      = printerType === "prusa";
  const isMoonraker  = printerType === "moonraker";
  const isHttpPrinter = isPrusa || isMoonraker;

  // Stats bar
  const statsEl = document.getElementById("print-opts-stats");
  const hasStats = meta.print_time || meta.layers || meta.filament_g || meta.filament_mm;
  statsEl.style.display = hasStats ? "" : "none";
  if (hasStats) {
    document.getElementById("print-opts-stat-time-val").textContent =
      meta.print_time ? formatTimeLong(meta.print_time) : "--";

    let filamentText = "--";
    if (meta.filament_g != null)
      filamentText = `${parseFloat(meta.filament_g).toFixed(2)}g`;
    else if (meta.filament_mm != null)
      filamentText = `${Math.round(meta.filament_mm)}mm`;
    document.getElementById("print-opts-stat-filament-val").textContent = filamentText;

    document.getElementById("print-opts-stat-layers-val").textContent =
      meta.layers != null ? meta.layers : "--";
  }

  document.getElementById("print-opts-filename").textContent = fileName;
  document.getElementById("print-opt-timelapse").checked = false;
  document.getElementById("print-opt-leveling").checked  = false;
  _setPrintPlate(0);


  // CC1 and CC2 support leveling/timelapse/plate; Prusa/Moonraker handle these natively
  document.getElementById("print-opts-cc1-only").style.display = isHttpPrinter ? "none" : "";

  // Load thumbnail
  const thumbImg  = document.getElementById("print-opts-thumb");
  const thumbWrap = document.getElementById("print-opts-thumb-wrap");
  thumbImg.style.display = "none";
  thumbImg.src = "";
  if (isHttpPrinter) {
    thumbWrap.style.display = "none";
  } else if (isCC2) {
    // CC2: thumbnail arrives async via WS file_info message; request it now
    thumbWrap.style.display = "none";
    send({ action: "get_file_info", printer_id: _currentFilePrinterId, filename: fileName });
  } else {
    const bare = fileName.endsWith(".png") ? fileName : fileName + ".png";
    thumbImg.onload  = () => { thumbImg.style.display = ""; };
    thumbImg.onerror = () => { thumbImg.style.display = "none"; thumbWrap.style.display = "none"; };
    thumbWrap.style.display = "";
    thumbImg.src = `/api/thumbnail/${encodeURIComponent(_currentFilePrinterId)}/${encodeURIComponent(bare)}`;
  }

  document.getElementById("modal-print-opts").classList.add("open");
}

function closePrintOpts() {
  document.getElementById("modal-print-opts").classList.remove("open");
  _printOptsFile = null;
  _printOptsName = null;
  _printOptsFilename = null;
}

function _setPrintPlate(plateId) {
  document.getElementById("print-plate-textured").className =
    "btn print-plate-btn " + (plateId === 0 ? "btn-primary" : "btn-secondary");
  document.getElementById("print-plate-smooth").className =
    "btn print-plate-btn " + (plateId === 1 ? "btn-primary" : "btn-secondary");
  document.getElementById("print-plate-textured").dataset.selected = plateId === 0 ? "1" : "";
  document.getElementById("print-plate-smooth").dataset.selected   = plateId === 1 ? "1" : "";
}

document.getElementById("print-plate-textured").addEventListener("click", () => _setPrintPlate(0));
document.getElementById("print-plate-smooth").addEventListener("click",   () => _setPrintPlate(1));

document.getElementById("btn-print-opts-cancel").addEventListener("click", closePrintOpts);
document.getElementById("modal-print-opts").addEventListener("click", e => {
  if (e.target === document.getElementById("modal-print-opts")) closePrintOpts();
});

document.getElementById("btn-print-opts-send").addEventListener("click", () => {
  if (!_printOptsFile) return;
  const opts = {
    timelapse:    document.getElementById("print-opt-timelapse").checked,
    leveling:     document.getElementById("print-opt-leveling").checked,
    smooth_plate: document.getElementById("print-plate-smooth").dataset.selected === "1",
  };
  send({
    action:     "start_print",
    printer_id: _currentFilePrinterId,
    filename:   _printOptsFile,
    print_opts: opts,
  });
  closePrintOpts();
  closeFileBrowser();
  toast(`Starting: ${_printOptsName}`);
});

document.getElementById("btn-files-close").addEventListener("click", closeFileBrowser);
document.getElementById("btn-files-refresh").addEventListener("click", () => {
  if (!_currentFilePrinterId) return;
  document.getElementById("files-list").innerHTML = "";
  document.getElementById("files-loading").style.display = "";
  send({ action: "list_files", printer_id: _currentFilePrinterId });
});
document.getElementById("modal-files").addEventListener("click", (e) => {
  if (e.target === document.getElementById("modal-files")) closeFileBrowser();
});

// ─── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const area = document.getElementById("toast-area");
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function toastAction(msg, btnLabel, onClick, duration = 8000) {
  const area = document.getElementById("toast-area");
  const el = document.createElement("div");
  el.className = "toast toast-action";
  const span = document.createElement("span");
  span.textContent = msg;
  const btn = document.createElement("button");
  btn.className = "toast-btn";
  btn.textContent = btnLabel;
  btn.addEventListener("click", () => { el.remove(); onClick(); });
  el.appendChild(span);
  el.appendChild(btn);
  area.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ─── Helpers ───────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function escAttr(s) {
  return String(s ?? "").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

// ─── History panel ────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const r = await fetch("/api/history");
    history = (await r.json()).reverse(); // newest first
    renderHistory();
  } catch (e) { /* server may not be ready yet */ }
}


// ─── API access (keys for other programs) ────────────────────────────────────
let _apiTokens = [];

async function _loadApiPage() {
  document.getElementById("api-address").textContent = `${location.origin}/api/external/v1/`;
  document.getElementById("api-enabled").checked = featureEnabled("external_api");
  document.getElementById("api-new-key").hidden = true;
  try {
    const r = await fetch("/api/api-tokens");
    if (r.ok) _apiTokens = (await r.json()).tokens || [];
  } catch (_) { /* keep the previous list */ }
  _renderApiTokens();
}

function _renderApiTokens() {
  const host = document.getElementById("api-token-list");
  if (!_apiTokens.length) {
    host.replaceChildren(_el("p", "stats-empty", "No keys yet."));
    return;
  }
  const rows = _apiTokens.map(t => {
    const row = _el("div", "api-key-row");
    const info = _el("div", "api-key-info");
    info.append(_el("strong", null, t.name));
    info.append(_el("span", "api-key-meta",
      `${t.scope === "write" ? "Read + change references" : "Read only"} · ${t.hint} · created ${_fmtWhen(t.created).slice(0, 10)} · ` +
      (t.last_used ? `last used ${_fmtWhen(t.last_used)}` : "never used")));
    const del = _el("button", "btn btn-secondary btn-sm", "Revoke");
    del.type = "button";
    del.addEventListener("click", async () => {
      if (!confirm(`Revoke "${t.name}"? Any program using this key stops working.`)) return;
      const r = await fetch(`/api/api-tokens/${encodeURIComponent(t.id)}`, { method: "DELETE" });
      if (r.ok) { toast("Key revoked"); _loadApiPage(); } else toast("Could not revoke the key", true);
    });
    row.append(info, del);
    return row;
  });
  host.replaceChildren(...rows);
}

document.getElementById("btn-settings-goto-api")?.addEventListener("click", () => {
  _showSettingsPage(_settingsApiPage);
  _loadApiPage();
});
document.getElementById("btn-settings-back-api")?.addEventListener("click", _backToSettingsMenu);

document.getElementById("api-enabled")?.addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  if (enabled && !confirm("Allow other programs to read your print history and pictures with an API key? Keys are created below; nothing is reachable without one.")) {
    e.target.checked = false;
    return;
  }
  try {
    const r = await fetch("/api/features", { method: "PATCH", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ key: "external_api", enabled }) });
    const d = await r.json();
    if (!r.ok) { toast(d.error || "Could not change this", true); e.target.checked = !enabled; return; }
    _applyFeatures(d.features);
    toast(enabled ? "API access is on" : "API access is off");
  } catch (err) { toast("Could not change this: " + err.message, true); e.target.checked = !enabled; }
});

document.getElementById("btn-api-create")?.addEventListener("click", async () => {
  const name = document.getElementById("api-key-name").value.trim();
  const scope = document.getElementById("api-key-scope").value;
  const r = await fetch("/api/api-tokens", { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify({ name, scope }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || "Could not create the key", true); return; }
  document.getElementById("api-key-name").value = "";
  const box = document.getElementById("api-new-key");
  const code = _el("code", null, d.key);
  const copy = _el("button", "btn btn-secondary btn-sm", "Copy");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(d.key); toast("Key copied"); }
    catch (_) { const sel = getSelection(); const rng = document.createRange(); rng.selectNodeContents(code); sel.removeAllRanges(); sel.addRange(rng); toast("Select and copy the key"); }
  });
  box.replaceChildren(_el("strong", null, `Key for "${d.token.name}" — copy it now`),
                      _el("p", "report-desc", "This is the only time it is shown. If you lose it, revoke it and create a new one."),
                      code, copy);
  box.hidden = false;
  if (!featureEnabled("external_api")) toast("The key is created, but API access is still switched off above", true);
  _apiTokens.push(d.token);
  _renderApiTokens();
});


// ─── Login-off warning ───────────────────────────────────────────────────────
// With AUTH_ENABLED=false anyone on the network can use Spooler; say so, for as
// long as that is the case (footer, and on the Features page).
async function loadSecurityStatus() {
  try {
    const r = await fetch("/api/security-status");
    if (!r.ok) return;
    const { auth_enabled } = await r.json();
    document.getElementById("auth-warning").hidden = auth_enabled !== false;
    document.getElementById("features-auth-warning").hidden = auth_enabled !== false;
  } catch (_) { /* server may not be ready yet */ }
}
document.getElementById("auth-warning")?.addEventListener("click", (e) => {
  e.preventDefault();
  _openSettings();
  _showSettingsPage(_settingsFeaturesPage);
  _renderFeaturesList();
});

// ─── Feature flags ───────────────────────────────────────────────────────────
async function loadFeatures() {
  try {
    const r = await fetch("/api/features");
    if (r.ok) _applyFeatures(await r.json());
  } catch (e) { /* server may not be ready yet */ }
}

// ─── Integrations config (T8) ────────────────────────────────────────────────
let integrations = { fields: [], tests: {}, server_settings: {} };

function integrationField(key) {
  return integrations.fields.find(f => f.key === key);
}

async function loadIntegrations() {
  try {
    const r = await fetch("/api/integrations");
    if (!r.ok) return;
    integrations = await r.json();
    const slicerUrl = integrationField("slicer.url")?.value || "";
    const slicerBtn = document.getElementById("btn-slicer");
    if (slicerBtn) {
      slicerBtn.hidden = !slicerUrl;
      if (slicerUrl) slicerBtn.href = slicerUrl;
    }
    if (_settingsIntegrationsPage && _settingsIntegrationsPage.style.display !== "none") {
      _renderIntegrationsList();
    }
  } catch (e) { /* server may not be ready yet */ }
}

const _INTEGRATION_GROUPS = [
  { title: "Spoolman", prefix: "spoolman.", testKey: "spoolman" },
  { title: "Slicer",   prefix: "slicer.",   testKey: null },
];

// Static markup in index.html; re-attached into the Spoolman group after each render.
const _catalogueEl = document.querySelector(".integrations-catalogue");

function _renderIntegrationsList() {
  const container = document.getElementById("integrations-list");
  if (!container) return;

  container.innerHTML = _INTEGRATION_GROUPS.map(group => {
    const fields = integrations.fields.filter(f => f.key.startsWith(group.prefix));
    if (!fields.length) return "";
    const test = group.testKey ? integrations.tests[group.testKey] : null;

    const fieldsHtml = fields.map(f => {
      const sourceLabel = f.source === "env" ? "from environment variable"
                         : f.source === "ui"  ? "custom"
                         : "default";
      const lockedNote = f.locked
        ? `<div class="integration-locked-note">Locked by server configuration</div>` : "";

      if (f.type === "bool") {
        return `
          <div class="integration-field">
            <div style="display:flex;align-items:center;gap:10px;justify-content:space-between">
              <span>${escHtml(f.label)}</span>
              <label class="toggle-switch">
                <input type="checkbox" data-config-key="${escAttr(f.key)}"
                       ${f.value ? "checked" : ""} ${f.locked ? "disabled" : ""} />
                <span class="toggle-slider"></span>
              </label>
            </div>
            <div class="integration-field-meta"><span class="integration-source-badge">${escHtml(sourceLabel)}</span></div>
            ${lockedNote}
          </div>`;
      }

      const inputType = f.type === "secret" ? "password" : f.type === "int" ? "number" : "text";
      const placeholder = f.type === "secret" ? (f.set ? "•••• (leave blank to keep)" : "Not set") : "";
      const value = f.type === "secret" ? "" : escAttr(f.value ?? "");
      const removeBtn = (f.type === "secret" && f.set)
        ? `<button type="button" class="btn btn-secondary btn-sm" data-clear-key="${escAttr(f.key)}">Remove</button>` : "";
      return `
        <div class="integration-field">
          <label>${escHtml(f.label)}
            <input type="${inputType}" data-config-key="${escAttr(f.key)}" value="${value}"
                   placeholder="${escAttr(placeholder)}" ${f.locked ? "disabled" : ""} />
          </label>
          <div class="integration-field-meta">
            <span class="integration-source-badge">${escHtml(sourceLabel)}</span>
            ${removeBtn}
          </div>
          ${lockedNote}
        </div>`;
    }).join("");

    const testHtml = group.testKey ? `
      <div class="integration-test-row">
        <button type="button" class="btn btn-secondary btn-sm" data-test-key="${escAttr(group.testKey)}">Test connection</button>
        ${test ? `<span class="integration-test-result ${test.ok ? "ok" : "fail"}">${escHtml(test.message)}</span>` : ""}
      </div>` : "";

    return `
      <details class="integration-group" ${group.prefix === "spoolman." ? "open" : ""}>
        <summary>${escHtml(group.title)}</summary>
        <div class="integration-group-body">${fieldsHtml}${testHtml}</div>
      </details>`;
  }).join("");

  const spoolmanBody = container.querySelector(".integration-group .integration-group-body");
  if (_catalogueEl && spoolmanBody && container.querySelector(".integration-group summary")?.textContent === "Spoolman") {
    spoolmanBody.appendChild(_catalogueEl);
  }

  container.querySelectorAll("button[data-test-key]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.testKey;
      btn.disabled = true;
      btn.textContent = "Testing…";
      try {
        const r = await fetch(`/api/integrations/${encodeURIComponent(key)}/test`, { method: "POST" });
        integrations.tests[key] = await r.json();
      } catch (e) {
        integrations.tests[key] = { ok: false, message: e.message };
      }
      _renderIntegrationsList();
    });
  });

  container.querySelectorAll("button[data-clear-key]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.clearKey;
      try {
        const r = await fetch("/api/integrations", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ clear: [key] }),
        });
        const data = await r.json();
        if (!r.ok) { toast(data.error || "Could not clear field", true); return; }
        integrations.fields = data.fields;
        _renderIntegrationsList();
        toast("Cleared");
      } catch (e) {
        toast("Could not clear field: " + e.message, true);
      }
    });
  });
}

function _renderServerSettings() {
  const el = document.getElementById("integrations-server-settings-list");
  if (!el) return;
  const s = integrations.server_settings || {};
  el.innerHTML = Object.entries(s)
    .map(([k, v]) => `<span>${escHtml(k)}</span><b>${escHtml(String(v))}</b>`)
    .join("");
}

document.getElementById("btn-integrations-save")?.addEventListener("click", async () => {
  const values = {};
  document.querySelectorAll("#integrations-list [data-config-key]").forEach(input => {
    const key = input.dataset.configKey;
    values[key] = input.type === "checkbox" ? input.checked : input.value;
  });
  try {
    const r = await fetch("/api/integrations", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const data = await r.json();
    if (!r.ok) {
      toast(data.error || "Could not save", true);
      return;
    }
    toast("Saved");
    await loadIntegrations();
    _renderIntegrationsList();
  } catch (e) {
    toast("Could not save: " + e.message, true);
  }
});

// Settings menu items (and their sub-pages) that hide entirely when their
// feature is off, instead of just rejecting on the server -- so the user
// never sees a settings entry for something that's been switched off.
const _GATED_SETTINGS_PAGES = [
  { feature: "backup",        btnId: "btn-settings-goto-backup",        page: () => _settingsBackupPage },
  { feature: "notifications", btnId: "btn-settings-goto-notifications", page: () => _settingsNotifPage },
];

function _applyFeatures(list) {
  features = {};
  list.forEach(f => { features[f.key] = f; });
  Object.values(printers).forEach(renderPrinter);
  if (_currentFilePrinterId) updateUploadUi();
  const reportBtn = document.getElementById("btn-report-problem");
  if (reportBtn) reportBtn.style.display = featureEnabled("report_problem") ? "" : "none";
  const statsBtn = document.getElementById("btn-stats");
  if (statsBtn) statsBtn.style.display = featureEnabled("statistics") ? "" : "none";
  const spoolsBtn = document.getElementById("btn-spools");
  if (spoolsBtn) spoolsBtn.style.display = featureEnabled("spoolman") ? "" : "none";

  _GATED_SETTINGS_PAGES.forEach(({ feature, btnId, page }) => {
    const btn = document.getElementById(btnId);
    const enabled = featureEnabled(feature);
    if (btn) btn.style.display = enabled ? "" : "none";
    // If that page is open when its feature gets turned off (e.g. toggled
    // from another tab), don't leave the user stranded on a now-hidden page.
    const pageEl = page();
    if (!enabled && pageEl && pageEl.style.display !== "none") {
      _backToSettingsMenu();
    }
  });

  if (_settingsFeaturesPage && _settingsFeaturesPage.style.display !== "none") {
    _renderFeaturesList();
  }
}

const _FEATURE_GROUPS = [
  { title: "Monitoring",    keys: ["camera", "print_snapshot"] },
  { title: "Notifications", keys: ["notifications"] },
  { title: "Data",          keys: ["backup", "statistics"] },
  { title: "Integrations",  keys: ["spoolman"] },
];

function _renderFeaturesList() {
  const container = document.getElementById("features-list");
  if (!container) return;
  container.innerHTML = _FEATURE_GROUPS.map(group => {
    const rows = group.keys.filter(k => features[k]).map(key => {
      const f = features[key];
      const lockedNote = f.locked
        ? `<div class="feature-locked-note">${escHtml(f.lock_reason || "Locked off by server configuration")}</div>`
        : "";
      return `
        <div class="feature-row">
          <div class="feature-info">
            <span class="feature-label">${escHtml(f.name)}</span>
            <span class="feature-desc">${escHtml(f.description)}</span>
            ${lockedNote}
          </div>
          <label class="toggle-switch">
            <input type="checkbox" data-feature-key="${escAttr(key)}"
                   ${f.enabled ? "checked" : ""} ${f.locked ? "disabled" : ""} />
            <span class="toggle-slider"></span>
          </label>
        </div>`;
    }).join("");
    return rows ? `<div class="feature-group-title">${escHtml(group.title)}</div>${rows}` : "";
  }).join("");

  container.querySelectorAll("input[data-feature-key]").forEach(input => {
    input.addEventListener("change", async () => {
      const key = input.dataset.featureKey;
      const enabled = input.checked;
      if (enabled && features[key]?.risky && !confirm(`${features[key].name} is marked risky. Enable it?`)) {
        input.checked = false;
        return;
      }
      try {
        const r = await fetch("/api/features", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key, enabled }),
        });
        const data = await r.json();
        if (!r.ok) {
          toast(data.error || "Could not update feature", true);
          input.checked = !enabled;
          return;
        }
        _applyFeatures(data.features);
      } catch (e) {
        toast("Could not update feature: " + e.message, true);
        input.checked = !enabled;
      }
    });
  });
}

// The print list lives in the Stats panel ("Prints" tab); see renderPrints().
function renderHistory() { renderPrints(); }

// Full-size picture of a finished print (click or Esc to close).
function openSnapshot(id) {
  closeSnapshot();
  const box = document.createElement("div");
  box.id = "snap-lightbox";
  box.className = "snap-lightbox";
  box.innerHTML = `<img src="/api/snapshot/${escAttr(id)}" alt="Picture of the finished print" />`;
  box.addEventListener("click", closeSnapshot);
  document.body.appendChild(box);
}
function closeSnapshot() { document.getElementById("snap-lightbox")?.remove(); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSnapshot(); });

const historyBackdrop = document.getElementById("panel-backdrop");   // shared dimmer behind side panels
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSpools(); closeStats(); closePrinters(); closeFileBrowser(); closePrintOpts(); } });


// ─── Statistics (T14) ─────────────────────────────────────────────────────────
// Numbers come from /api/stats (stats.py); this only draws them. The chart is
// inline SVG: single series, so the app accent is the one mark colour, bars are
// capped at 24px with a 4px rounded top, grid lines are hairlines, and every
// value is also reachable through the tooltip and the table view.
const statsPanel = document.getElementById("panel-stats");
const btnStats   = document.getElementById("btn-stats");
let _statsRange  = "30";
let _statsMetric = "grams";
let _statsView   = "chart";
let _statsTab    = "overview";     // "overview" | "prints"
let _openPrintId = null;           // the print whose detail view is open, if any
let _statsData   = null;

const _STATS_METRICS = {
  grams:   { label: "Filament (g)", unit: "g",  fmt: v => _fmtNum(v, v < 10 ? 1 : 0) },
  prints:  { label: "Prints",       unit: "",   fmt: v => _fmtNum(v, 0) },
  hours:   { label: "Print time (h)", unit: "h", fmt: v => _fmtNum(v, v < 10 ? 1 : 0) },
};

function _fmtNum(v, digits = 0) {
  return Number(v ?? 0).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits });
}
function _isoDay(d) {
  const z = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
}
function _statsPeriod() {
  if (_statsRange === "custom") {
    return { from: document.getElementById("stats-from").value, to: document.getElementById("stats-to").value };
  }
  if (_statsRange === "all") return { from: "", to: "" };
  const to = new Date(), from = new Date();
  if (_statsRange === "365") { from.setMonth(from.getMonth() - 11); from.setDate(1); }
  else from.setDate(from.getDate() - (parseInt(_statsRange, 10) - 1));
  return { from: _isoDay(from), to: _isoDay(to) };
}
function _statsQuery() {
  const { from, to } = _statsPeriod();
  const q = new URLSearchParams();
  if (from) q.set("from", from);
  if (to) q.set("to", to);
  const pr = document.getElementById("stats-printer").value;
  if (pr) q.set("printer", pr);
  return q.toString();
}

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;     // names come from files/printers: never innerHTML
  return e;
}

async function loadStats() {
  const body = document.getElementById("stats-body");
  body.style.opacity = _statsData ? ".5" : "1";      // keep the previous frame while reloading
  const q = _statsQuery();
  document.getElementById("btn-stats-csv").href = "/api/history.csv" + (q ? "?" + q : "");
  try {
    const r = await fetch("/api/stats" + (q ? "?" + q : ""));
    if (!r.ok) throw new Error(r.status);
    _statsData = await r.json();
    renderStats();
  } catch (e) {
    body.replaceChildren(_el("p", "stats-empty", "Could not load statistics."));
  } finally {
    body.style.opacity = "1";
  }
}

function _fmtHours(h) {
  return h >= 100 ? `${_fmtNum(h, 0)} h` : `${_fmtNum(h, 1)} h`;
}

function _statTile(label, value, sub) {
  const t = _el("div", "stat-tile");
  t.append(_el("div", "stat-tile-label", label), _el("div", "stat-tile-value", value));
  if (sub) t.append(_el("div", "stat-tile-sub", sub));
  return t;
}

function _niceStep(rough) {
  const exp = Math.pow(10, Math.floor(Math.log10(rough)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= rough) return m * exp;
  return 10 * exp;
}

function _bucketLabel(key, kind) {
  const d = new Date(kind === "month" ? key + "-01T00:00" : key + "T00:00");
  return kind === "month"
    ? d.toLocaleDateString(undefined, { month: "short", year: "2-digit" })
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
function _bucketTitle(key, kind) {
  const d = new Date(kind === "month" ? key + "-01T00:00" : key + "T00:00");
  if (kind === "month") return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const label = d.toLocaleDateString(undefined, { weekday: kind === "day" ? "short" : undefined, day: "numeric", month: "short", year: "numeric" });
  return kind === "week" ? `Week of ${label}` : label;
}

const _SVGNS = "http://www.w3.org/2000/svg";
function _svg(tag, attrs) {
  const e = document.createElementNS(_SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}

function _renderStatsChart(d, tip) {
  const metric = _STATS_METRICS[_statsMetric];
  const series = d.series;
  const W = 640, H = 230, L = 44, R = 8, T = 10, B = 26;
  const pw = W - L - R, ph = H - T - B;
  const vals = series.map(b => b[_statsMetric]);
  const max = Math.max(0, ...vals);
  const step = max > 0 ? _niceStep(max / 4) : 1;
  const ymax = Math.max(step, step * Math.ceil(max / step));
  const y = v => T + ph - (v / ymax) * ph;

  const svg = _svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "stats-chart", role: "group",
                            "aria-label": `${metric.label} per ${d.bucket}` });
  for (let v = 0; v <= ymax + 1e-9; v += step) {
    svg.append(_svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid" }));
    const t = _svg("text", { x: L - 6, y: y(v) + 4, class: "axis", "text-anchor": "end" });
    t.textContent = _fmtNum(v, step < 1 ? 1 : 0);
    svg.append(t);
  }
  const n = series.length, slot = pw / Math.max(1, n);
  const bw = Math.min(24, Math.max(2, slot * 0.62));
  const every = Math.max(1, Math.ceil(52 / slot));
  const hits = [];
  series.forEach((b, i) => {
    const cx = L + slot * i + slot / 2;
    const v = b[_statsMetric];
    if (v > 0) {
      const h = Math.max(2, (v / ymax) * ph), x = cx - bw / 2, top = T + ph - h, r = Math.min(4, h, bw / 2);
      svg.append(_svg("path", { class: "bar", "data-i": i,
        d: `M${x},${T + ph} V${top + r} a${r},${r} 0 0 1 ${r},${-r} H${x + bw - r} a${r},${r} 0 0 1 ${r},${r} V${T + ph} Z` }));
    }
    if (i % every === 0) {
      const lt = _svg("text", { x: cx, y: H - 8, class: "axis", "text-anchor": "middle" });
      lt.textContent = _bucketLabel(b.key, d.bucket);
      svg.append(lt);
    }
    // Hit area: the whole slot, full height -- never just the painted bar.
    const hit = _svg("rect", { x: L + slot * i, y: T, width: slot, height: ph, class: "hit", tabindex: 0,
      role: "img", "aria-label": `${_bucketTitle(b.key, d.bucket)}: ${b.prints} prints, ${_fmtNum(b.grams, 1)} g, ${_fmtNum(b.hours, 1)} h` });
    hits.push(hit);
    const show = () => {
      svg.querySelectorAll(".bar.hover").forEach(e => e.classList.remove("hover"));
      svg.querySelector(`.bar[data-i="${i}"]`)?.classList.add("hover");
      tip.replaceChildren(_el("div", "tip-title", _bucketTitle(b.key, d.bucket)));
      for (const [lab, val] of [["Filament", `${_fmtNum(b.grams, 1)} g`], ["Prints", _fmtNum(b.prints)], ["Print time", `${_fmtNum(b.hours, 1)} h`]]) {
        const row = _el("div", "tip-row");
        row.append(_el("strong", null, val), _el("span", null, lab));
        tip.append(row);
      }
      tip.hidden = false;
      const wrap = svg.parentElement, box = wrap.getBoundingClientRect(), sv = svg.getBoundingClientRect();
      const px = (cx / W) * sv.width + (sv.left - box.left);
      tip.style.left = Math.min(Math.max(px - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth) + "px";
      tip.style.top = "0px";
    };
    const hide = () => { tip.hidden = true; svg.querySelectorAll(".bar.hover").forEach(e => e.classList.remove("hover")); };
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", show);
    hit.addEventListener("blur", hide);
  });
  hits.forEach(h => svg.append(h));
  return svg;
}

function _statsTable(d) {
  const tbl = _el("table", "stats-table");
  const head = _el("tr");
  ["Period", "Prints", "Filament (g)", "Print time (h)"].forEach(t => head.append(_el("th", null, t)));
  const thead = _el("thead");
  thead.append(head);
  tbl.append(thead);
  const tb = _el("tbody");
  d.series.forEach(b => {
    const tr = _el("tr");
    tr.append(_el("td", null, _bucketTitle(b.key, d.bucket)), _el("td", null, _fmtNum(b.prints)),
              _el("td", null, _fmtNum(b.grams, 1)), _el("td", null, _fmtNum(b.hours, 1)));
    tb.append(tr);
  });
  tbl.append(tb);
  return tbl;
}

function _barList(title, rows, unit) {
  const card = _el("div", "stats-card");
  card.append(_el("h4", null, title));
  if (!rows.length) { card.append(_el("p", "stats-empty", "Nothing yet")); return card; }
  const max = Math.max(...rows.map(r => r.value), 1);
  rows.forEach(r => {
    const row = _el("div", "bar-row");
    const top = _el("div", "bar-row-top");
    top.append(_el("span", "bar-row-label", r.label), _el("strong", null, r.text));
    const track = _el("div", "bar-track");
    const fill = _el("div", "bar-fill");
    fill.style.width = Math.max(2, (r.value / max) * 100) + "%";
    track.append(fill);
    row.append(top, track);
    if (r.sub) row.append(_el("div", "bar-row-sub", r.sub));
    card.append(row);
  });
  return card;
}

function renderStats() {
  const d = _statsData, body = document.getElementById("stats-body");
  if (!d) return;
  const frag = document.createDocumentFragment();

  if (!d.prints) {
    frag.append(_el("p", "stats-empty", "No prints in this period."));
    body.replaceChildren(frag);
    return;
  }

  const tiles = _el("div", "stat-tiles");
  tiles.append(
    _statTile("Prints", _fmtNum(d.prints), `${d.results.complete} done · ${d.results.cancelled} stopped · ${d.results.error} failed`),
    _statTile("Success rate", d.success_rate == null ? "—" : `${_fmtNum(d.success_rate, 0)}%`, "finished without being stopped"),
    _statTile("Print time", _fmtHours(d.hours), d.avg_print_s ? `avg ${formatTime(d.avg_print_s)} per finished print` : ""),
    _statTile("Filament", d.grams >= 1000 ? `${_fmtNum(d.grams / 1000, 2)} kg` : `${_fmtNum(d.grams, 0)} g`, "includes stopped prints"),
  );
  frag.append(tiles);

  // Chart card: metric switch + chart/table switch
  const card = _el("div", "stats-card stats-chart-card");
  const bar = _el("div", "stats-chart-bar");
  bar.append(_el("h4", null, `${_STATS_METRICS[_statsMetric].label} per ${d.bucket}`));
  const seg = _el("div", "seg seg-sm");
  Object.entries(_STATS_METRICS).forEach(([k, m]) => {
    const b = _el("button", k === _statsMetric ? "active" : "", k === "grams" ? "Filament" : k === "prints" ? "Prints" : "Time");
    b.type = "button";
    b.addEventListener("click", () => { _statsMetric = k; renderStats(); });
    seg.append(b);
  });
  const view = _el("div", "seg seg-sm");
  [["chart", "Chart"], ["table", "Table"]].forEach(([k, t]) => {
    const b = _el("button", k === _statsView ? "active" : "", t);
    b.type = "button";
    b.addEventListener("click", () => { _statsView = k; renderStats(); });
    view.append(b);
  });
  bar.append(seg, view);
  card.append(bar);
  if (_statsView === "table") {
    const tw = _el("div", "stats-table-wrap");
    tw.append(_statsTable(d));
    card.append(tw);
  } else {
    const wrap = _el("div", "stats-chart-wrap");
    const tip = _el("div", "stats-tip");
    tip.hidden = true;
    wrap.append(_renderStatsChart(d, tip), tip);
    card.append(wrap);
  }
  frag.append(card);

  const mats = Object.entries(d.by_material).map(([k, v]) => ({ label: k, value: v, text: `${_fmtNum(v, 0)} g` }));
  frag.append(_barList("Filament by material", mats));
  const prs = Object.entries(d.by_printer).map(([k, v]) => ({
    label: k, value: v.grams, text: `${_fmtNum(v.grams, 0)} g`, sub: `${v.prints} prints · ${_fmtNum(v.hours, 1)} h` }));
  frag.append(_barList("By printer", prs));

  const res = _el("div", "stats-card");
  res.append(_el("h4", null, "Results"));
  [["complete", "Finished", "✓", "ok"], ["cancelled", "Stopped", "–", "muted"], ["error", "Failed", "!", "bad"]].forEach(([k, label, icon, cls]) => {
    const n = d.results[k], row = _el("div", "result-row");
    row.append(_el("span", `result-icon ${cls}`, icon), _el("span", "result-label", label),
               _el("strong", null, `${n}  (${_fmtNum(100 * n / d.prints, 0)}%)`));
    res.append(row);
  });
  frag.append(res);

  const why = Object.entries(d.failure_reasons);
  if (why.length) {
    const wc = _el("div", "stats-card");
    wc.append(_el("h4", null, "Why prints stopped"));
    why.slice(0, 8).forEach(([reason, n]) => {
      const row = _el("div", "result-row");
      row.append(_el("span", "result-label", _REASON_CATEGORY_LABEL[reason] || reason), _el("strong", null, String(n)));
      wc.append(row);
    });
    frag.append(wc);
  }
  body.replaceChildren(frag);
}

function _fillStatsPrinters() {
  const sel = document.getElementById("stats-printer");
  const cur = sel.value;
  sel.replaceChildren(new Option("All printers", ""));
  Object.values(printers).forEach(p => sel.add(new Option(p.name || p.id, p.id)));
  sel.value = [...sel.options].some(o => o.value === cur) ? cur : "";
}

function _showStatsTab(tab) {
  _statsTab = tab;
  document.querySelectorAll("#stats-tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  const detail = tab === "prints" && _openPrintId;
  document.getElementById("stats-body").hidden   = tab !== "overview";
  document.getElementById("prints-body").hidden  = tab !== "prints" || !!detail;
  document.getElementById("print-detail").hidden = !detail;
  document.getElementById("stats-search").hidden = tab !== "prints";
  document.getElementById("stats-filterbar").hidden = !!detail;
  document.getElementById("stats-tabs").hidden = !!detail;
  if (tab === "overview") loadStats(); else renderPrints();
}

const openStats = () => {
  statsPanel.classList.add("open");
  historyBackdrop.classList.add("open");
  btnStats.classList.add("active");
  _fillStatsPrinters();
  _showStatsTab(_statsTab);
};
const closeStats = () => {
  statsPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  btnStats.classList.remove("active");
};
const _refreshStats = () => { if (_statsTab === "overview") loadStats(); else renderPrints(); };
btnStats.addEventListener("click", openStats);
document.getElementById("btn-stats-close").addEventListener("click", closeStats);
document.getElementById("stats-printer").addEventListener("change", _refreshStats);
document.getElementById("stats-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) { _openPrintId = null; _showStatsTab(b.dataset.tab); }
});
document.getElementById("stats-search").addEventListener("input", () => renderPrints());
document.getElementById("stats-range").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-range]");
  if (!b) return;
  _statsRange = b.dataset.range;
  document.querySelectorAll("#stats-range button").forEach(x => x.classList.toggle("active", x === b));
  document.getElementById("stats-custom").hidden = _statsRange !== "custom";
  if (_statsRange === "custom") {
    const { from, to } = { from: document.getElementById("stats-from"), to: document.getElementById("stats-to") };
    if (!to.value) to.value = _isoDay(new Date());
    if (!from.value) { const d = new Date(); d.setDate(d.getDate() - 29); from.value = _isoDay(d); }
  }
  _refreshStats();
});
["stats-from", "stats-to"].forEach(id => document.getElementById(id).addEventListener("change", _refreshStats));

// ─── Prints list and per-print detail (the old History, now inside Stats) ────
function _endStateOf(e) {
  if (e.end_state === "complete" || e.end_state === "cancelled" || e.end_state === "error") return e.end_state;
  return e.completed ? "complete" : "cancelled";
}
const _RESULT_UI = {
  complete:  { label: "Finished", icon: "✓", cls: "ok" },
  cancelled: { label: "Stopped",  icon: "–", cls: "muted" },
  error:     { label: "Failed",   icon: "!", cls: "bad" },
};

function _visiblePrints() {
  const { from, to } = _statsPeriod();
  const pr = document.getElementById("stats-printer").value;
  const q = (document.getElementById("stats-search").value || "").trim().toLowerCase();
  return history.filter(e => {
    const day = (e.timestamp || "").slice(0, 10);
    if (from && day < from) return false;
    if (to && day > to) return false;
    if (pr && e.printer_id !== pr) return false;
    if (q && !`${e.filename || ""} ${e.reference || ""}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

function _printThumb(e) {
  if (e.snapshot && e.id && featureEnabled("print_snapshot")) {
    const img = _el("img", "print-thumb");
    img.src = `/api/snapshot/${e.id}`;
    img.alt = "Picture of the print";
    img.loading = "lazy";
    return img;
  }
  return _el("div", "print-thumb print-thumb-empty", "—");
}

function renderPrints() {
  if (_statsTab !== "prints") return;
  if (_openPrintId) { _renderPrintDetail(); return; }
  const host = document.getElementById("prints-body");
  const rows = _visiblePrints();
  const frag = document.createDocumentFragment();
  const g = rows.reduce((s, e) => s + (e.filament_g || 0), 0);
  frag.append(_el("div", "prints-count", rows.length
    ? `${rows.length} print${rows.length === 1 ? "" : "s"} · ${_fmtNum(g, 0)} g`
    : (history.length ? "No prints match." : "No prints logged yet.")));
  rows.forEach(e => {
    const st = _RESULT_UI[_endStateOf(e)];
    const row = _el("button", "print-row");
    row.type = "button";
    row.addEventListener("click", () => { _openPrintId = e.id; _showStatsTab("prints"); });
    const main = _el("div", "print-row-main");
    const title = _el("div", "print-row-title");
    title.append(_el("span", "print-row-name", e.filename || "Unnamed print"));
    if (e.reference) title.append(_el("span", "ref-badge", "#" + e.reference));
    const meta = _el("div", "print-row-meta",
      `${(e.timestamp || "").replace("T", " ").slice(0, 16)} · ${e.printer_name || "Printer"} · ${formatTime(e.print_time_s || 0)} · ${_fmtNum(e.filament_g || 0, 1)} g`);
    main.append(title, meta);
    const icon = _el("span", `result-icon ${st.cls}`, st.icon);
    icon.title = st.label;
    icon.setAttribute("aria-label", st.label);
    row.append(_printThumb(e), main, icon);
    frag.append(row);
  });
  host.replaceChildren(frag);
}

function _fact(label, value) {
  const f = _el("div", "fact");
  f.append(_el("dt", null, label), _el("dd", null, value));
  return f;
}
function _fmtWhen(ts) { return ts ? ts.replace("T", " ").slice(0, 16) : "—"; }

function _renderPrintDetail() {
  const host = document.getElementById("print-detail");
  const e = history.find(x => x.id === _openPrintId);
  if (!e) { _openPrintId = null; _showStatsTab("prints"); return; }
  const st = _RESULT_UI[_endStateOf(e)];
  const frag = document.createDocumentFragment();

  const back = _el("button", "btn btn-secondary btn-sm", "← Back to prints");
  back.type = "button";
  back.addEventListener("click", () => { _openPrintId = null; _showStatsTab("prints"); });
  frag.append(back);

  const head = _el("div", "print-detail-head");
  head.append(_el("h4", null, e.filename || "Unnamed print"));
  const badge = _el("span", `result-pill ${st.cls}`, `${st.icon} ${st.label}`);
  head.append(badge);
  frag.append(head);

  if (e.snapshot && e.id && featureEnabled("print_snapshot")) {
    const img = _el("img", "print-photo");
    img.src = `/api/snapshot/${e.id}`;
    img.alt = "Picture taken when the print ended";
    img.addEventListener("click", () => openSnapshot(e.id));
    frag.append(img);
  } else {
    frag.append(_el("div", "print-photo print-photo-empty", "No picture was saved for this print"));
  }

  // Reference number
  const ref = _el("div", "ref-edit");
  const lab = _el("label", null, "Reference number");
  const input = _el("input");
  input.type = "text"; input.maxLength = 40; input.value = e.reference || ""; input.placeholder = "e.g. 042";
  input.id = "print-ref-input";
  lab.append(input);
  const save = _el("button", "btn btn-primary btn-sm", "Save");
  save.type = "button";
  const doSave = async () => {
    save.disabled = true;
    try {
      const r = await fetch(`/api/history/${encodeURIComponent(e.id)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reference: input.value }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { toast(d.error || "Could not save the reference", true); return; }
      e.reference = d.reference;
      input.value = d.reference;
      toast(d.reference ? `Reference saved: ${d.reference}` : "Reference cleared");
    } catch (err) {
      toast("Could not save the reference: " + err.message, true);
    } finally { save.disabled = false; }
  };
  save.addEventListener("click", doSave);
  input.addEventListener("keydown", ev => { if (ev.key === "Enter") doSave(); });
  ref.append(lab, save);
  frag.append(ref);

  // Facts
  const facts = _el("dl", "facts");
  facts.append(
    _fact("Printer", e.printer_name || "—"),
    _fact("Started", _fmtWhen(e.started_at)),
    _fact("Ended", _fmtWhen(e.timestamp)),
    _fact("Print time", formatTime(e.print_time_s || 0)),
    _fact("Filament", `${_fmtNum((e.filament_mm || 0) / 1000, 2)} m · ${_fmtNum(e.filament_g || 0, 1)} g`),
  );
  if (e.material || e.vendor) facts.append(_fact("Material", [e.vendor, e.material].filter(Boolean).join(" ")));
  if (Array.isArray(e.spools) && e.spools.length) {
    const names = e.spools.map(sp => {
      const full = (typeof spools !== "undefined" ? spools : []).find(x => x.id === sp.id);
      return `${full ? spoolName(full) : "Spool #" + sp.id}: ${_fmtNum(sp.g, 1)} g`;
    });
    facts.append(_fact(e.spools.length > 1 ? "Spools used" : "Spool", names.join("\n")));
  }
  if (_endStateOf(e) !== "complete") {
    const cause = e.error_message || _REASON_CATEGORY_LABEL[e.stop_reason] || e.stop_reason || "Cause not known";
    const by = e.initiated_by ? ` (${_REASON_INITIATOR_LABEL[e.initiated_by] || e.initiated_by})` : "";
    facts.append(_fact("Cause", cause + (e.error_code ? ` · code ${e.error_code}` : "") + by));
  }
  frag.append(facts);

  if (Array.isArray(e.pauses) && e.pauses.length) {
    const pc = _el("div", "stats-card");
    pc.append(_el("h4", null, `Pauses (${e.pauses.length})`));
    e.pauses.forEach(p => {
      const row = _el("div", "result-row");
      const dur = p.duration_s != null ? ` · ${formatTime(p.duration_s)}` : "";
      const who = _REASON_INITIATOR_LABEL[p.initiated_by] || p.initiated_by || "";
      row.append(_el("span", "result-label", `${_fmtWhen(p.since).slice(11)}${dur} — ${_REASON_CATEGORY_LABEL[p.category] || p.category || "paused"}${who ? " (" + who + ")" : ""}`));
      pc.append(row);
    });
    frag.append(pc);
  }
  host.replaceChildren(frag);
}

// ─── Spoolman / Spools ────────────────────────────────────────────────────────
async function fetchSpools() {
  try {
    const r = await fetch(`${SPOOLMAN_URL}/spool`);
    if (!r.ok) return;
    spools = await r.json();
    Object.values(printers).forEach(p => renderPrinter(p));
    renderSpoolPanel();
  } catch (_) { /* Spoolman not running */ }
}

function renderSpoolPanel() {
  const list  = document.getElementById("spools-list");
  const empty = document.getElementById("spools-empty");
  if (!spools.length) {
    list.innerHTML = "";
    empty.style.display = "";
    return;
  }
  empty.style.display = "none";
  list.innerHTML = spools.map(s => {
    const pct        = spoolPct(s);
    const remaining  = spoolRemaining(s);
    const total      = spoolTotal(s);
    const color      = spoolColorHex(s);
    const material   = s.filament?.material || "?";
    const vendor     = s.filament?.vendor?.name || "";
    const colorName  = s.filament?.name || "";
    const assignedTo = spoolAssignedTo(s);
    const printerName = assignedTo ? (printerByLocation(assignedTo)?.name || assignedTo) : null;
    const isEmpty    = remaining === 0;
    const isLow      = !isEmpty && total > 0 && pct < 10;
    const barColor   = isEmpty ? "var(--red)" : isLow ? "var(--yellow)" : null;

    return `<div class="spool-card${isEmpty ? " spool-empty" : isLow ? " spool-low" : ""}">
      <div class="spool-card-swatch" style="background:${escAttr(color)}"></div>
      <div class="spool-card-body">
        <div class="spool-card-top">
          <div class="spool-card-vendor">${escHtml(vendor || "Unknown brand")}</div>
          <span class="spool-material-badge">${escHtml(material)}</span>
        </div>
        <div class="spool-card-colorname">${escHtml(colorName || "—")}</div>
        <div class="spool-weight-row">
          <span>${remaining}g / ${total}g</span>
          <span class="spool-weight-pct">${pct}%${isEmpty ? ' <span class="spool-tag empty">Empty</span>' : isLow ? ' <span class="spool-tag low">Low</span>' : ""}</span>
        </div>
        <div class="spool-bar-wrap">
          <div class="spool-bar-fill" style="width:${pct}%${barColor ? ";background:" + barColor : ""}"></div>
        </div>
        <div class="spool-card-footer">
          ${printerName ? `<span class="spool-printer-badge">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>
            </svg>
            ${escHtml(printerName)}
          </span>` : "<span></span>"}
          <button class="btn btn-sm btn-secondary" onclick="deleteSpool('${escAttr(s.id)}')">Remove</button>
        </div>
      </div>
    </div>`;
  }).join("");
}

function openSpoolPicker(printerId) {
  currentPickerPrinterId = printerId;
  currentPickerTrayId    = null;
  const printer = printers[printerId];
  document.getElementById("spool-picker-title").textContent =
    `Assign Spool – ${printer?.name || printerId}`;
  renderPickerList(printerId);
  document.getElementById("modal-spool-picker").classList.add("open");
}

function openTrayPicker(printerId, trayId) {
  currentPickerPrinterId = printerId;
  currentPickerTrayId    = trayId;
  const printer = printers[printerId];
  document.getElementById("spool-picker-title").textContent =
    `Link Spool – ${printer?.name || printerId} · Slot ${trayId + 1}`;
  renderPickerList(printerId);
  document.getElementById("modal-spool-picker").classList.add("open");
}

function renderPickerList(printerId) {
  const list   = document.getElementById("spool-picker-list");
  const isTray = currentPickerTrayId != null;

  // Classify each spool relative to the current context
  const currentLinked = isTray
    ? (trayMap[printerId] || {})[String(currentPickerTrayId)]
    : spools.find(s => spoolAssignedTo(s) === printerId)?.id;

  function spoolRow(s, onClickFn, isSelected) {
    const loc          = spoolAssignedTo(s);
    const locPrinter   = printerByLocation(loc);
    const otherPrinter = loc && locPrinter?.id !== printerId ? locPrinter : null;
    const activePrinter = getSpoolActivePrinter(s.id);  // printer currently printing with this spool
    const inUse        = activePrinter != null;
    const pct          = spoolPct(s);
    const badge        = inUse
      ? `<span class="spool-pick-badge in-use">Active · ${escHtml(activePrinter.name)}</span>`
      : otherPrinter
        ? `<span class="spool-pick-badge elsewhere">On · ${escHtml(otherPrinter.name)}</span>`
        : "";
    if (inUse) {
      return `<div class="spool-pick-item spool-pick-blocked" title="Currently loaded on ${escAttr(activePrinter.name)} — unload filament before reassigning">
        <div class="spool-dot" style="background:${escAttr(spoolColorHex(s))};opacity:.4"></div>
        <div class="spool-pick-info">
          <div class="spool-pick-name" style="opacity:.5">${escHtml(spoolName(s))}</div>
          <div class="spool-pick-meta">${escHtml(s.filament?.material || "")} · ${spoolRemaining(s)}g (${pct}%) ${badge}</div>
        </div>
      </div>`;
    }
    return `<div class="spool-pick-item${isSelected ? " selected" : ""}${otherPrinter ? " spool-pick-elsewhere" : ""}"
                 onclick="${onClickFn}">
      <div class="spool-dot" style="background:${escAttr(spoolColorHex(s))}"></div>
      <div class="spool-pick-info">
        <div class="spool-pick-name">${escHtml(spoolName(s))}</div>
        <div class="spool-pick-meta">${escHtml(s.filament?.material || "")} · ${spoolRemaining(s)}g (${pct}%) ${badge}</div>
      </div>
      ${isSelected ? '<span class="spool-check">✓</span>' : ""}
    </div>`;
  }

  const noneLabel  = isTray ? "None – unlink" : "None – unassign";
  const noneClick  = isTray
    ? `linkTray('${escAttr(printerId)}', ${currentPickerTrayId}, null)`
    : `assignSpool('${escAttr(printerId)}', null)`;
  const noneSelected = currentLinked == null;

  const rows = spools.map(s => {
    const isSelected = s.id === currentLinked || (!isTray && spoolAssignedTo(s) === printerId);
    const click = isTray
      ? `linkTray('${escAttr(printerId)}', ${currentPickerTrayId}, ${s.id})`
      : `assignSpool('${escAttr(printerId)}', '${escAttr(s.id)}')`;
    return spoolRow(s, click, isSelected);
  });

  list.innerHTML = `
    <div class="spool-pick-item${noneSelected ? " selected" : ""}" onclick="${noneClick}">
      <div class="spool-dot" style="background:var(--border)"></div>
      <div class="spool-pick-info"><div class="spool-pick-name">${noneLabel}</div></div>
      ${noneSelected ? '<span class="spool-check">✓</span>' : ""}
    </div>
    ${rows.join("")}
  `;
}

async function assignSpool(printerId, spoolId) {
  const loc = printerLocation(printerId);
  try {
    // Unassign existing spool on this printer if it's different
    const cur = spools.find(s => printerByLocation(spoolAssignedTo(s))?.id === printerId);
    if (cur && cur.id !== spoolId) {
      await fetch(`${SPOOLMAN_URL}/spool/${cur.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ location: "" }),
      });
    }
    if (spoolId) {
      await fetch(`${SPOOLMAN_URL}/spool/${spoolId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ location: loc }),
      });
    }
    document.getElementById("modal-spool-picker").classList.remove("open");
    await fetchSpools();
    toast(spoolId ? "Spool assigned" : "Spool unassigned");
  } catch (e) {
    toast("Failed to assign spool", true);
  }
}

function linkTray(printerId, trayId, spoolId) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      action:     "link_tray",
      printer_id: printerId,
      tray_id:    trayId,
      spool_id:   spoolId,
    }));
  }
  document.getElementById("modal-spool-picker").classList.remove("open");
  toast(spoolId != null ? `Slot ${trayId + 1} linked` : `Slot ${trayId + 1} unlinked`);
}

async function deleteSpool(spoolId) {
  if (!confirm("Remove this spool?")) return;
  try {
    // Find the filament/vendor IDs before deleting the spool
    const spool = spools.find(s => s.id == spoolId);
    const filamentId = spool?.filament?.id;
    const vendorId   = spool?.filament?.vendor?.id;

    const r = await fetch(`${SPOOLMAN_URL}/spool/${spoolId}`, { method: "DELETE" });
    if (!r.ok) throw new Error(`${r.status}`);

    // Clean up the filament (may fail if shared – that's fine)
    if (filamentId) {
      await fetch(`${SPOOLMAN_URL}/filament/${filamentId}`, { method: "DELETE" });
    }
    // Clean up the vendor if it has no remaining filaments
    if (vendorId) {
      const vf = await fetch(`${SPOOLMAN_URL}/filament?vendor_id=${vendorId}`);
      if (vf.ok && (await vf.json()).length === 0) {
        await fetch(`${SPOOLMAN_URL}/vendor/${vendorId}`, { method: "DELETE" });
      }
    }

    await fetchSpools();
    toast("Spool removed");
  } catch (e) {
    toast("Failed to remove spool: " + e.message, true);
  }
}


// ─── Filament deductions waiting for Spoolman (T22) ───────────────────────────
// Spooler writes every deduction to a ledger before sending it; this shows the
// ones Spoolman hasn't taken (yet), lets you retry or discard them, and puts a
// badge on the Spools button when something needs a person.
let _ledgerItems = [];

function _ledgerStatusText(e) {
  if (e.status === "discarded") return `Not deducted — ${e.last_error || "discarded"}`;
  if (e.status === "sending") return "Sending…";
  if (e.status === "failed" && e.next_try_at == null) return "Waiting for your decision";
  if (e.status === "failed") return `Failed ${e.attempts}× — will try again ${_ledgerWhen(e.next_try_at)}`;
  return "Waiting to be sent";
}
function _ledgerWhen(epoch) {
  const s = Math.round(epoch - _serverNowS());
  if (s <= 5) return "shortly";
  if (s < 90) return `in ${s}s`;
  if (s < 5400) return `in ${Math.round(s / 60)} min`;
  return `in ${Math.round(s / 3600)} h`;
}

function renderLedger() {
  const card = document.getElementById("ledger-card");
  if (!card) return;
  if (!_ledgerItems.length) { card.hidden = true; card.replaceChildren(); return; }
  const unsent = _ledgerItems.filter(e => ["pending", "sending", "failed"].includes(e.status));
  card.hidden = false;
  const head = _el("div", "ledger-head");
  head.append(_el("strong", null, unsent.length
    ? `${unsent.length} filament deduction${unsent.length === 1 ? "" : "s"} not yet in Spoolman`
    : "Recent filament that was not deducted"));
  const rows = _ledgerItems.map(e => {
    const row = _el("div", `ledger-row ${e.status}`);
    const sp = e.spool_id != null ? (spools.find(x => x.id === e.spool_id) || null) : null;
    const spoolText = e.spool_id == null ? "unknown spool" : (sp ? spoolName(sp) : `spool #${e.spool_id}`);
    const main = _el("div", "ledger-main");
    main.append(_el("div", "ledger-title", `${_fmtNum(e.grams, 1)} g from ${spoolText}`));
    main.append(_el("div", "ledger-meta", `${e.printer_name || "Printer"}${e.filename ? " · " + e.filename : ""} · ${new Date(e.created_at * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}`));
    main.append(_el("div", "ledger-state", _ledgerStatusText(e)));
    if (e.status === "failed" && e.last_error) main.append(_el("div", "ledger-error", e.last_error));
    if (e.note) main.append(_el("div", "ledger-meta", e.note));
    row.append(main);
    if (["pending", "failed"].includes(e.status)) {
      const retry = _el("button", "btn btn-secondary btn-sm", "Try again now");
      retry.type = "button";
      retry.addEventListener("click", () => _ledgerAction(e.id, "retry"));
      const drop = _el("button", "btn btn-secondary btn-sm", "Discard");
      drop.type = "button";
      drop.addEventListener("click", () => {
        if (confirm(`Discard ${_fmtNum(e.grams, 1)} g from ${spoolText}? Spoolman will NOT be told about this filament.`)) _ledgerAction(e.id, "discard");
      });
      const act = _el("div", "ledger-actions");
      act.append(retry, drop);
      row.append(act);
    }
    return row;
  });
  card.replaceChildren(head, ...rows);
}

async function _ledgerAction(id, action) {
  try {
    const r = await fetch(`/api/spoolman-ledger/${encodeURIComponent(id)}${action === "retry" ? "/retry" : ""}`,
                          { method: action === "retry" ? "POST" : "DELETE" });
    if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.error || "Could not do that", true); }
    else toast(action === "retry" ? "Trying again" : "Discarded");
  } catch (e) { toast("Could not do that: " + e.message, true); }
  loadLedger();
}

function _setLedgerBadge(summary) {
  const b = document.getElementById("spools-badge");
  if (!b) return;
  const n = summary?.attention || 0;
  b.hidden = n === 0;
  b.textContent = n ? String(n) : "";
}

async function loadLedger() {
  if (!featureEnabled("spoolman")) { _ledgerItems = []; renderLedger(); _setLedgerBadge(null); return; }
  try {
    const r = await fetch("/api/spoolman-ledger");
    if (!r.ok) return;
    const d = await r.json();
    _ledgerItems = d.items || [];
    _setLedgerBadge(d.summary);
    renderLedger();
  } catch (_) { /* server may not be ready yet */ }
}

// Spools side panel
const spoolsPanel = document.getElementById("panel-spools");
const btnSpools   = document.getElementById("btn-spools");

const openSpools = () => {
  spoolsPanel.classList.add("open");
  historyBackdrop.classList.add("open");
  btnSpools.classList.add("active");
  fetchSpools();
  loadLedger();
};
const closeSpools = () => {
  spoolsPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  btnSpools.classList.remove("active");
};

btnSpools.addEventListener("click", openSpools);
document.getElementById("btn-spools-close").addEventListener("click", closeSpools);

async function loadImportBrands() {
  const sel = document.getElementById("import-brand");
  if (!sel || sel.dataset.loaded) return;
  try {
    const r = await fetch("/api/filament-meta");
    if (!r.ok) throw new Error(r.status);
    const { brands } = await r.json();
    if (!brands.length) throw new Error("empty");
    sel.innerHTML = brands.map(b => `<option value="${escAttr(b)}">${escHtml(b)}</option>`).join("");
    const elegoo = brands.find(b => b.toLowerCase() === "elegoo");
    if (elegoo) sel.value = elegoo;
    sel.dataset.loaded = "1";
  } catch (_) {
    sel.innerHTML = '<option value="">Could not load brands</option>';
  }
}

document.getElementById("btn-import-filaments").addEventListener("click", async () => {
  const btn = document.getElementById("btn-import-filaments");
  const brand = document.getElementById("import-brand").value;
  if (!brand) { toast("Pick a brand first", true); return; }
  btn.disabled = true;
  btn.textContent = "Importing…";
  try {
    const r = await fetch("/api/import-filaments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brand }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    if (d.created > 0) {
      alert(`✓ Imported ${d.created} ${brand} filaments into Spoolman.\n\nYou can now create spools from these in Spoolman UI or when adding a spool here.`);
    } else {
      alert(`All ${d.total} ${brand} filaments are already in Spoolman (${d.skipped} skipped).`);
    }
  } catch (e) {
    alert("Import failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Import";
  }
});

// Close all panels on backdrop click
historyBackdrop.addEventListener("click", () => { closeSpools(); closeStats(); closePrinters(); });

// Add Spool modal
const addSpoolModal = document.getElementById("modal-add-spool");

// "Other…" toggle for brand and material dropdowns
document.getElementById("spool-input-brand").addEventListener("change", (e) => {
  document.getElementById("spool-input-brand-other").style.display =
    e.target.value === "__other__" ? "" : "none";
});
document.getElementById("spool-input-material").addEventListener("change", (e) => {
  document.getElementById("spool-input-material-other").style.display =
    e.target.value === "__other__" ? "" : "none";
  // Auto-fill density when a known material is selected
  const density = _materialDensityMap[e.target.value];
  if (density) document.getElementById("spool-input-density").value = density;
});

function getSpoolSelectValue(selectId, otherId) {
  const sel = document.getElementById(selectId).value;
  if (sel === "__other__") return document.getElementById(otherId).value.trim();
  return sel;
}

document.getElementById("btn-add-spool").addEventListener("click", () => {
  loadFilamentMeta();
  addSpoolModal.classList.add("open");
});
document.getElementById("btn-add-spool-cancel").addEventListener("click", () => {
  addSpoolModal.classList.remove("open");
});
addSpoolModal.addEventListener("click", (e) => {
  if (e.target === addSpoolModal) addSpoolModal.classList.remove("open");
});

document.getElementById("btn-add-spool-confirm").addEventListener("click", async () => {
  const brand    = getSpoolSelectValue("spool-input-brand", "spool-input-brand-other");
  const material = getSpoolSelectValue("spool-input-material", "spool-input-material-other") || "PLA";
  const color    = document.getElementById("spool-input-color").value.trim();
  const hex      = (document.getElementById("spool-input-hex").value || "#888888").replace(/^#/, "");
  const weight   = parseFloat(document.getElementById("spool-input-weight").value) || 1000;
  const diameter = parseFloat(document.getElementById("spool-input-diameter").value) || 1.75;
  const density  = parseFloat(document.getElementById("spool-input-density").value) || 1.24;

  if (!material) { toast("Select a material", true); return; }

  try {
    // Step 1: find or create vendor
    let vendorId = null;
    if (brand) {
      const vr = await fetch(`${SPOOLMAN_URL}/vendor?name=${encodeURIComponent(brand)}`);
      if (!vr.ok) throw new Error("Failed to fetch vendors");
      const vendors = await vr.json();
      if (vendors.length > 0) {
        vendorId = vendors[0].id;
      } else {
        const cv = await fetch(`${SPOOLMAN_URL}/vendor`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: brand }),
        });
        if (!cv.ok) throw new Error(await cv.text());
        vendorId = (await cv.json()).id;
      }
    }

    // Step 2: create filament
    const filamentBody = { material, weight, color_hex: hex, density, diameter };
    if (color) filamentBody.name = color;
    if (vendorId) filamentBody.vendor_id = vendorId;
    const fr = await fetch(`${SPOOLMAN_URL}/filament`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(filamentBody),
    });
    if (!fr.ok) throw new Error(await fr.text());
    const filament = await fr.json();

    // Step 3: create spool
    const sr = await fetch(`${SPOOLMAN_URL}/spool`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filament_id: filament.id, initial_weight: weight }),
    });
    if (!sr.ok) throw new Error(await sr.text());

    addSpoolModal.classList.remove("open");
    // Reset form
    document.getElementById("spool-input-ean").value = "";
    document.getElementById("spool-input-brand").value = "";
    document.getElementById("spool-input-material").value = "";
    document.getElementById("spool-input-brand-other").style.display = "none";
    document.getElementById("spool-input-brand-other").value = "";
    document.getElementById("spool-input-material-other").style.display = "none";
    document.getElementById("spool-input-material-other").value = "";
    document.getElementById("spool-input-color").value = "";
    document.getElementById("spool-input-hex").value = "#888888";
    document.getElementById("spool-input-weight").value = "";
    document.getElementById("spool-input-diameter").value = "1.75";
    document.getElementById("spool-input-density").value = "1.24";
    await fetchSpools();
    toast("Spool added");
  } catch (e) {
    toast("Failed to add spool: " + e.message, true);
  }
});

// ─── EAN lookup + barcode scanner ─────────────────────────────────────────────

async function lookupEan(ean) {
  if (!ean) return;
  document.getElementById("spool-input-ean").value = ean;
  try {
    const r = await fetch(`/api/lookup-ean?ean=${encodeURIComponent(ean)}`);
    if (r.status === 404) { toast(`EAN ${ean} not found in database`, true); return; }
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();

    const brand    = d.manufacturer || "";
    const material = d.material     || "";
    const color    = d.color_name   || "";
    const hex      = d.color_hex ? "#" + d.color_hex.replace(/^#/, "") : "#888888";
    const weight   = d.weight ?? 1000;

    // Brand dropdown
    const bSel = document.getElementById("spool-input-brand");
    const bOther = document.getElementById("spool-input-brand-other");
    if ([...bSel.options].some(o => o.value === brand)) {
      bSel.value = brand; bOther.style.display = "none";
    } else if (brand) {
      bSel.value = "__other__"; bOther.value = brand; bOther.style.display = "";
    }

    // Material dropdown
    const mSel = document.getElementById("spool-input-material");
    const mOther = document.getElementById("spool-input-material-other");
    if ([...mSel.options].some(o => o.value === material)) {
      mSel.value = material; mOther.style.display = "none";
    } else if (material) {
      mSel.value = "__other__"; mOther.value = material; mOther.style.display = "";
    }

    document.getElementById("spool-input-color").value  = color;
    document.getElementById("spool-input-hex").value    = hex;
    document.getElementById("spool-input-weight").value = weight;

    toast(`Found: ${d.name || [brand, material, color].filter(Boolean).join(" ")}`);
  } catch (e) {
    toast("Lookup failed: " + e.message, true);
  }
}

// EAN field: lookup on Enter
document.getElementById("spool-input-ean").addEventListener("keydown", (e) => {
  if (e.key === "Enter") lookupEan(e.target.value.trim());
});
// Lookup on paste after short delay (scanner keyboards send paste then Enter)
document.getElementById("spool-input-ean").addEventListener("input", (e) => {
  const v = e.target.value.trim();
  if (v.length >= 8 && /^\d+$/.test(v)) {
    clearTimeout(e.target._eanTimer);
    e.target._eanTimer = setTimeout(() => lookupEan(v), 300);
  }
});

// Camera scanner
let _liveScanner = null;

function _setScannerError(msg, opts = {}) {
  const { sub, photo = false } = opts;
  document.getElementById("scanner-region").style.display = "none";
  document.getElementById("scanner-hint").style.display = "none";
  document.getElementById("scanner-error-msg").textContent = msg;
  if (sub !== undefined) document.getElementById("scanner-error-sub").textContent = sub;
  document.getElementById("scanner-error").style.display = "flex";
  document.getElementById("btn-scanner-photo").style.display = photo ? "" : "none";
}

function _resetScannerError() {
  document.getElementById("scanner-region").style.display = "";
  document.getElementById("scanner-hint").style.display = "";
  document.getElementById("scanner-error").style.display = "none";
  document.getElementById("scanner-error-sub").textContent = "Enter the EAN code manually instead.";
  document.getElementById("btn-scanner-photo").style.display = "none";
}

async function openScanner() {
  _resetScannerError();
  document.getElementById("modal-scanner").classList.add("open");

  if (!window.isSecureContext) {
    _setScannerError("Live scanner requires HTTPS", {
      sub: "Tap 'Take Photo' to scan a barcode with your camera, or enter the code manually.",
      photo: true
    });
    return;
  }

  // Guard against start() hanging indefinitely (e.g. PC with no camera attached)
  let timedOut = false;
  const giveUpTimer = setTimeout(() => {
    timedOut = true;
    const s = _liveScanner;
    _liveScanner = null;
    s?.stop().catch(() => {}).finally(() => { try { s.clear(); } catch {} });
    _setScannerError("No camera found on this device");
  }, 5000);

  try {
    _liveScanner = new Html5Qrcode("scanner-region", { verbose: false });
    await _liveScanner.start(
      { facingMode: "environment" },
      { fps: 10, qrbox: { width: 280, height: 100 } },
      (text) => { clearTimeout(giveUpTimer); closeScanner(); lookupEan(text); },
      () => {}
    );
    clearTimeout(giveUpTimer);
  } catch (e) {
    clearTimeout(giveUpTimer);
    if (timedOut) return;
    const s = _liveScanner;
    _liveScanner = null;
    s?.stop().catch(() => {}).finally(() => { try { s.clear(); } catch {} });
    _setScannerError(e.name === "NotFoundError" ? "No camera found on this device" : "Camera not available");
  }
}

function closeScanner() {
  document.getElementById("modal-scanner").classList.remove("open");
  if (_liveScanner) {
    const s = _liveScanner;
    _liveScanner = null;
    s.stop()
      .catch(() => {})
      .finally(() => { try { s.clear(); } catch {} });
  }
}

// File input: decode image from camera photo
document.getElementById("barcode-file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    const reader = new Html5Qrcode("barcode-scan-canvas", { verbose: false });
    const text = await reader.scanFile(file, false);
    lookupEan(text);
  } catch {
    toast("No barcode found in image – try again", true);
  }
});

document.getElementById("btn-scan-ean").addEventListener("click", openScanner);
document.getElementById("btn-scanner-cancel").addEventListener("click", closeScanner);
document.getElementById("btn-scanner-photo").addEventListener("click", () => {
  closeScanner();
  document.getElementById("barcode-file-input").click();
});
document.getElementById("modal-scanner").addEventListener("click", (e) => {
  if (e.target === document.getElementById("modal-scanner")) closeScanner();
});

// Spool picker cancel
document.getElementById("btn-spool-picker-cancel").addEventListener("click", () => {
  document.getElementById("modal-spool-picker").classList.remove("open");
});
document.getElementById("modal-spool-picker").addEventListener("click", (e) => {
  if (e.target === document.getElementById("modal-spool-picker"))
    document.getElementById("modal-spool-picker").classList.remove("open");
});

// ─── Changelog ────────────────────────────────────────────────────────────────
let _changelog = [];

async function loadChangelog() {
  try {
    const r = await fetch("/changelog.json?v=" + Date.now());
    const data = await r.json();
    if (!Array.isArray(data) || data.length === 0) return;
    _changelog = data;
    const badge = document.getElementById("version-badge");
    if (badge) badge.textContent = "v" + _changelog[0].version;
  } catch (_) {}
}

function openChangelog() {
  const body = document.getElementById("changelog-body");
  if (!body) return;
  body.innerHTML = _changelog.map(entry => `
    <div class="cl-entry">
      <div class="cl-entry-header">
        <span class="cl-version">v${entry.version}</span>
        <span class="cl-date">${entry.date}</span>
      </div>
      <ul class="cl-list">
        ${entry.changes.map(c => `<li>${escHtml(c)}</li>`).join("")}
      </ul>
    </div>`).join("");
  document.getElementById("modal-changelog")?.classList.add("open");
}

document.getElementById("version-badge")?.addEventListener("click", openChangelog);
document.getElementById("btn-changelog-close")?.addEventListener("click", () =>
  document.getElementById("modal-changelog")?.classList.remove("open"));
document.getElementById("modal-changelog")?.addEventListener("click", e => {
  if (e.target.id === "modal-changelog")
    e.target.classList.remove("open");
});

// ─── Demo mode (?demo=states) ───────────────────────────────────────────────────
// One synthetic card per state, side by side — lets anyone sanity-check every
// status dot/badge/reason-box combination at once without needing a printer
// in every possible state. No WebSocket connection is made in this mode.
function renderDemoStates() {
  const reasonFor = (s) => {
    if (s === "error") return { kind: "error", initiated_by: "printer", code: "99",
                                 category: "unknown", message: "Demo error message" };
    if (s === "pausing" || s === "paused") return { kind: "pause", initiated_by: "unknown",
                                 code: "", category: "unknown", message: "" };
    if (s === "cancelled" || s === "stopping") return { kind: "stop", initiated_by: "spooler",
                                 code: "", category: "unknown", message: "" };
    return null;
  };
  Object.keys(STATE_LABEL).forEach((s) => {
    const p = {
      id: `demo-${s}`, ip: "demo", name: `Demo: ${STATE_LABEL[s]}`,
      printer_type: "cc1", connected: s !== "offline",
      state: s, state_reason: reasonFor(s),
      status: { PrintInfo: { Filename: "demo.gcode", CurrentLayer: 10, TotalLayer: 100,
                              PrintTime: 600, RemainTime: 900, TotalExtrusion: 1200 } },
      attrs: {}, camera_url: null, filament_mm: 1200, filament_g: 3.6, has_access_code: false,
    };
    printers[p.id] = p;
    renderPrinter(p);
  });
}

// Mobile browsers (notably when installed as a PWA) suspend the network
// connection behind an MJPEG <img> while the app is backgrounded, and the
// stream doesn't resume on its own since the <img> src never changes -- it
// hangs showing the last frame (or the alt text) until something forces a
// fresh request. Re-point every camera <img> at a cache-busted URL whenever
// the app comes back to the foreground to force that reconnect.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  document.querySelectorAll('.card-camera img').forEach(img => {
    if (!img._baseUrl) return;
    const sep = img._baseUrl.includes('?') ? '&' : '?';
    img.src = `${img._baseUrl}${sep}_r=${Date.now()}`;
  });
});

// ─── Boot ──────────────────────────────────────────────────────────────────────
if (new URLSearchParams(location.search).get("demo") === "states") {
  renderDemoStates();
} else {
  fetch("/api/auth-status")
    .then(r => r.json())
    .then(({ spoolman_url }) => {
      if (spoolman_url) document.getElementById("btn-spoolman-ui").href = spoolman_url;
    })
    .catch(() => {});
  loadChangelog();
  loadFeatures();
  loadIntegrations();
  connect();
  // Staleness is purely a function of wall-clock time passing, not of new
  // data arriving — a card can go stale with no new printer_update at all,
  // so it needs its own tick independent of the WS message flow.
  setInterval(() => Object.values(printers).forEach(renderPrinter), 10000);
}

// On startup: if notifications are enabled but the subscription was cleared by the
// browser (e.g. after cache purge), silently re-subscribe so notifications keep working.
(async () => {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  try {
    const resp = await fetch("/api/notification-settings");
    if (!resp.ok) return;
    const s = await resp.json();
    if (!s || !Object.values(s).some(v => v?.enabled)) return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) await _subscribePush();
  } catch (_) {}
})();


// ─── Report problem ───────────────────────────────────────────────────────────
const _ISSUES_URL = "https://github.com/tharje/spooler-v2/issues/new";

const _URL_BUDGET = 7000; // GitHub rejects very long prefilled URLs

function _reportEnv(printerId) {
  const version = (typeof _changelog !== "undefined" && _changelog[0]) ? _changelog[0].version : "unknown";
  const list = printerId === "none" ? [] : Object.entries(printers).filter(([id]) => !printerId || id === printerId).map(([, p]) => p);
  const lines = list.map(p =>
    `- ${p.printer_type}${p.attrs?.Model ? " " + p.attrs.Model : ""}` +
    `${p.attrs?.FirmwareVersion ? ", firmware " + p.attrs.FirmwareVersion : ""}`);
  return [
    "**Environment**",
    `- Spooler version: ${version}`,
    `- Browser: ${navigator.userAgent}`,
    "- Printer:", ...(lines.length ? lines : ["- (none)"]),
  ].join("\n");
}

// Build title + body, trimming the oldest log lines until the URL fits.
function _reportIssueUrl() {
  const printerId = document.getElementById("report-printer").value;
  const title = document.getElementById("report-title").value.trim();
  const desc = document.getElementById("report-desc").value.trim();
  const diag = document.getElementById("report-diag").value;
  const head = `${desc || "_(no description)_"}\n\n${_reportEnv(printerId)}\n\n<details><summary>Diagnostics</summary>\n\n\`\`\`\n`;
  const tail = "\n```\n</details>\n";
  const make = (d) => `${_ISSUES_URL}?title=${encodeURIComponent("[Bug] " + title)}&body=${encodeURIComponent(head + d + tail)}`;
  let lines = diag.split("\n"), url = make(lines.join("\n")), cut = false;
  while (url.length > _URL_BUDGET && lines.length > 20) {
    lines = lines.slice(Math.ceil(lines.length * 0.15) || 1);
    cut = true;
    url = make("(older lines trimmed to fit - use Download to attach the full report)\n" + lines.join("\n"));
  }
  return url;
}

async function _loadDiagnostics() {
  const box = document.getElementById("report-diag");
  const id = document.getElementById("report-printer").value;
  box.value = "Loading…";
  try {
    const r = await fetch("/api/diagnostics?printer=" + encodeURIComponent(id));
    box.value = r.ok ? await r.text() : `Could not load diagnostics (HTTP ${r.status}).`;
  } catch (e) {
    box.value = "Could not load diagnostics: " + e;
  }
}

async function openReport() {
  const sel = document.getElementById("report-printer");
  const entries = Object.entries(printers);
  sel.innerHTML = "";
  if (entries.length > 1) sel.add(new Option("All printers", ""));
  entries.forEach(([id, p]) => sel.add(new Option(p.name || id, id)));
  sel.add(new Option("Not printer-related", "none"));
  document.getElementById("report-printer-row").hidden = sel.options.length <= 2;
  document.getElementById("report-title").value = "";
  document.getElementById("report-desc").value = "";
  document.getElementById("modal-report").classList.add("open");
  await _loadDiagnostics();
}

function closeReport() { document.getElementById("modal-report").classList.remove("open"); }

document.getElementById("btn-report-problem")?.addEventListener("click", openReport);
document.getElementById("btn-report-close")?.addEventListener("click", closeReport);
document.getElementById("modal-report")?.addEventListener("click", (e) => {
  if (e.target === document.getElementById("modal-report")) closeReport();
});
document.getElementById("report-printer")?.addEventListener("change", _loadDiagnostics);
document.getElementById("btn-report-github")?.addEventListener("click", () => {
  window.open(_reportIssueUrl(), "_blank", "noopener");
});
document.getElementById("btn-report-copy")?.addEventListener("click", async () => {
  const box = document.getElementById("report-diag");
  try { await navigator.clipboard.writeText(box.value); }
  catch (_) { box.select(); document.execCommand("copy"); }
  toast("Diagnostics copied");
});
document.getElementById("btn-report-download")?.addEventListener("click", () => {
  const blob = new Blob([document.getElementById("report-diag").value], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "spooler-diagnostics.txt";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});
