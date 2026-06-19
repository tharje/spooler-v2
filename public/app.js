/* Spooler – frontend app.js */
"use strict";

const WS_URL  = location.protocol === "https:"
  ? `wss://${location.hostname}:8766`
  : `ws://${location.hostname}:8765`;
const SPOOLMAN_URL = "/api/spoolman/api/v1";
const RECONNECT_DELAY = 3000;

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

// ─── Spoolman field helpers ────────────────────────────────────────────────────
function spoolName(s)       { return [s.filament?.vendor?.name, s.filament?.material, s.filament?.name].filter(Boolean).join(" ") || `Spool ${s.id}`; }
function spoolColorHex(s)   { const h = s.filament?.color_hex || "888888"; return h.startsWith("#") ? h : "#" + h; }
function spoolRemaining(s)  { return Math.round(s.remaining_weight ?? 0); }
function spoolTotal(s)      { return Math.round(s.initial_weight ?? 1000); }
function spoolPct(s)        { const t = spoolTotal(s); return t > 0 ? Math.round(spoolRemaining(s) / t * 100) : 0; }
function spoolAssignedTo(s) { return s.location || null; }

let currentPickerPrinterId = null;
let currentPickerTrayId    = null; // non-null → picker is in tray-link mode
let _currentFilePrinterId  = null;
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
function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log("[WS] Connected");
    send({ action: "list_printers" });
    loadHistory();
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
    setTimeout(connect, RECONNECT_DELAY);
  };

  ws.onerror = () => ws.close();
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

// ─── Message handling ──────────────────────────────────────────────────────────
function handleMessage(msg) {
  switch (msg.type) {
    case "printer_update":
      printers[msg.printer.id] = msg.printer;
      renderPrinter(msg.printer);
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
// PrintInfo.Status codes (from CarbonicSidecar / elegoo-homeassistant)
const PRINT_STATUS = {
  0:  "idle",
  1:  "homing",
  2:  "printing",    // bed dropping
  3:  "printing",
  4:  "printing",    // lifting
  5:  "pausing",
  6:  "paused",
  7:  "stopping",
  8:  "cancelled",
  9:  "complete",
  10: "checking",
  12: "recovering",
  13: "printing",    // printing (recovery)
  14: "cancelled",
  15: "warming up",
  16: "warming up",  // preheating
  18: "warming up",
  19: "warming up",
  20: "leveling",
  21: "warming up",
};

// CurrentStatus[0] codes (machine-level state)
const MACHINE_STATUS = {
  0: "idle", 1: "printing", 2: "transferring", 3: "testing",
  4: "testing", 5: "leveling", 6: "tuning", 7: "stopping",
  8: "stopped", 9: "homing", 10: "loading", 11: "tuning", 12: "recovering",
};

function getPrintStatus(printer) {
  if (!printer.connected) return "offline";
  const pi = printer.status?.PrintInfo;
  const code = pi?.Status;
  if (code === undefined || code === null) return "idle";
  // Special case: CurrentStatus[0] === 9 means homing (between prints)
  const machineCode = printer.status?.CurrentStatus?.[0];
  if (machineCode === 9 && code === 0) return "homing";
  return PRINT_STATUS[code] ?? "idle";
}

function statusClass(s) {
  if (["printing", "homing", "recovering"].includes(s))        return "printing";
  if (["warming up", "leveling", "checking"].includes(s))      return "warmingup";
  if (["pausing", "paused"].includes(s))                       return "paused";
  if (["stopping", "cancelled"].includes(s))                   return "cancelled";
  if (s === "complete")                                         return "complete";
  if (s === "offline")                                          return "offline";
  return "idle";
}

function isActivelyPrinting(printer) {
  const s = getPrintStatus(printer);
  return ["printing", "homing", "warming up", "leveling", "checking", "recovering"].includes(s);
}

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
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
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

  const nozzle     = printer.status?.TempOfNozzle    ?? printer.status?.NozzleTemp    ?? 0;
  const nozzleTgt  = printer.status?.TempTargetNozzle?? printer.status?.NozzleTempTarget ?? 0;
  const bed        = printer.status?.TempOfHotbed     ?? printer.status?.BedTemp       ?? 0;
  const bedTgt     = printer.status?.TempTargetHotbed ?? printer.status?.BedTempTarget ?? 0;
  const chamber    = printer.status?.TempOfBox        ?? printer.status?.ChamberTemp   ?? 0;
  const chamberTgt = printer.status?.TempTargetBox    ?? 0;

  const elapsed   = printer.status?.PrintInfo?.PrintTime  ?? 0;
  const remaining = printer.status?.PrintInfo?.RemainTime ?? 0;
  const filamentMm = printer.filament_mm ?? 0;
  const filamentG  = printer.filament_g  ?? 0;
  const lightOn    = getLightOn(printer);

  const cameraUrl = printer.camera_url
    ? `/api/camera/${encodeURIComponent(printer.id)}`
    : null;

  // Preserve the existing camera img element so its MJPEG stream connection
  // survives innerHTML replacement (every printer_update would kill it otherwise)
  const prevCameraImg = card.querySelector('.card-camera img');

  card.innerHTML = `
    <!-- Header -->
    <div class="card-header">
      <div class="status-dot ${sc}"></div>
      <div class="card-header-info">
        <div class="card-title">${escHtml(printer.name)}</div>
        <div class="card-subtitle">${escHtml(printer.ip)}${printer.attrs?.FirmwareVersion ? ` · fw ${escHtml(printer.attrs.FirmwareVersion)}` : ""}</div>
      </div>
      <span class="status-badge ${sc}">${status}</span>
      <button class="card-files-btn" onclick="openFileBrowser('${escAttr(printer.id)}')" title="Browse files">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
        </svg>
      </button>
      <button class="card-settings-btn" onclick="openPrinterSettings('${escAttr(printer.id)}')" title="Printer settings">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
        </svg>
      </button>
      <button class="card-remove-btn" onclick="removePrinter('${escAttr(printer.id)}')" title="Remove printer">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>
        </svg>
      </button>
    </div>

    <!-- Camera -->
    <div class="card-camera">
      <div class="camera-placeholder" style="display:flex">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
          <path d="M23 7l-7 5 7 5V7z"/>
          <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
        </svg>
        <span>${connected ? "No camera feed" : "Printer offline"}</span>
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
        <span>Remaining: ${formatTime(remaining)}</span>
      </div>
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
    ${!printer.status?.canvas_info ? (() => {
      const s = spools.find(sp => spoolAssignedTo(sp) === printer.id);
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
               <span>${escHtml(spoolName(linkedSpool).split(" ").slice(0,2).join(" "))}</span>
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
      <button class="btn btn-sm ${lightOn ? "btn-primary" : "btn-secondary"}"
              onclick="printerAction('${escAttr(printer.id)}','${lightOn ? "light_off" : "light_on"}')"
              title=""
              ${!connected ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="5"/>
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
        </svg>
      </button>
      ${(printing || paused) && printer.printer_type === "cc2" ? (() => {
        const sf = printer.status?.SpeedFactor ?? 100;
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
      cameraDiv.insertBefore(prevCameraImg, cameraDiv.firstChild);
      placeholder.style.display = 'none';
      prevCameraImg.style.display = '';
    } else {
      const img = document.createElement('img');
      img.src = cameraUrl;
      img.alt = 'Camera feed';
      img.addEventListener('load',  () => { placeholder.style.display = 'none'; });
      img.addEventListener('error', () => { img.style.display = 'none'; placeholder.style.display = 'flex'; });
      cameraDiv.insertBefore(img, cameraDiv.firstChild);
    }
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
  document.getElementById("input-type-readonly").textContent =
    p.printer_type === "cc2" ? "CC2 – Centauri Carbon 2 (MQTT)" : "CC1 – Centauri Carbon 1 (WebSocket/SDCP)";
  inputAccessCode.value = "";
  inputAccessCode.placeholder = p.has_access_code ? "Leave blank to keep current" : "Enter MQTT password";
  labelAccessCode.style.display = p.printer_type === "cc2" ? "flex" : "none";

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
const _settingsModal    = document.getElementById("modal-app-settings");
const _settingsMenu     = document.getElementById("settings-menu");
const _settingsPwPage   = document.getElementById("settings-change-password");
const _settingsNotifPage = document.getElementById("settings-notifications");

function _openSettings() {
  [_settingsPwPage, _settingsNotifPage].forEach(p => p && (p.style.display = "none"));
  _settingsMenu.style.display = "";
  const tog = document.getElementById("toggle-light-mode");
  if (tog) tog.checked = document.body.classList.contains("light-mode");
  _settingsModal?.classList.add("open");
}
function _showSettingsPage(pageEl) {
  _settingsMenu.style.display = "none";
  [_settingsPwPage, _settingsNotifPage].forEach(p => p && (p.style.display = "none"));
  pageEl.style.display = "";
}
function _backToSettingsMenu() {
  [_settingsPwPage, _settingsNotifPage].forEach(p => p && (p.style.display = "none"));
  _settingsMenu.style.display = "";
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

// Notifications sub-page UI
async function _populateNotifForm() {
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
document.getElementById("btn-notif-save")?.addEventListener("click", async () => {
  const gb = id => document.getElementById(id)?.checked ?? false;
  const gv = id => parseFloat(document.getElementById(id)?.value) || 0;
  const s = {
    finished:        { enabled: gb("notif-finished-on") },
    layer:           { enabled: gb("notif-layer-on"),           layer:     gv("notif-layer-number") },
    nozzle_idle:     { enabled: gb("notif-nozzle-idle-on"),     threshold: gv("notif-nozzle-idle-threshold") },
    nozzle_printing: { enabled: gb("notif-nozzle-printing-on"), threshold: gv("notif-nozzle-printing-threshold") },
    spool_low:       { enabled: gb("notif-spool-low-on"),       threshold: gv("notif-spool-low-threshold") },
  };
  const anyEnabled = Object.values(s).some(v => v.enabled);
  if (anyEnabled) {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { toast("Notification permission denied in browser settings"); return; }
    const sub = await _subscribePush();
    if (!sub) toast("Push subscription failed — notifications may not arrive when app is closed");
  } else {
    await _unsubscribePush();
  }
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

document.getElementById("btn-notif-test")?.addEventListener("click", async () => {
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
});

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
  labelAccessCode.style.display = inputType.value === "cc2" ? "flex" : "none";
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
  send({ action: "list_files", printer_id: printerId });
}

function closeFileBrowser() {
  document.getElementById("modal-files").classList.remove("open");
  _currentFilePrinterId = null;
}

function renderFileList(files, error) {
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

  const table = document.createElement("table");
  table.className = "files-table";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Name</th><th>Size</th><th></th></tr>";
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const f of files) {
    const tr = document.createElement("tr");

    const nameTd = document.createElement("td");
    nameTd.className = "file-name";
    nameTd.textContent = f.name || f.path || "";
    nameTd.title = f.path || "";
    tr.appendChild(nameTd);

    const sizeTd = document.createElement("td");
    sizeTd.className = "file-size";
    sizeTd.textContent = f.is_dir ? "—" : formatFileSize(f.size);
    tr.appendChild(sizeTd);

    const actionTd = document.createElement("td");
    actionTd.className = "file-actions";
    if (!f.is_dir) {
      const filePath = f.path;
      const fileName = f.name || filePath.split("/").pop() || filePath;

      const btn = document.createElement("button");
      btn.className = "btn btn-primary btn-sm";
      btn.textContent = "Print";
      btn.addEventListener("click", () => {
        if (confirm(`Print "${fileName}"?`)) {
          send({ action: "start_print", printer_id: _currentFilePrinterId, filename: filePath });
          closeFileBrowser();
          toast(`Starting: ${fileName}`);
        }
      });
      actionTd.appendChild(btn);

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
      actionTd.appendChild(delBtn);
    }
    tr.appendChild(actionTd);
    tbody.appendChild(tr);
  }

  table.appendChild(tbody);
  list.innerHTML = "";
  list.appendChild(table);
}

function formatFileSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${(bytes / 1073741824).toFixed(2)} GB`;
}

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

function renderHistory() {
  const tbody  = document.getElementById("history-tbody");
  const empty  = document.getElementById("history-empty");
  const totals = document.getElementById("history-totals");

  if (!history.length) {
    tbody.innerHTML = "";
    empty.style.display = "";
    totals.innerHTML = "";
    return;
  }
  empty.style.display = "none";

  // Totals
  const totalG  = history.reduce((s, e) => s + (e.filament_g  || 0), 0);
  const totalMm = history.reduce((s, e) => s + (e.filament_mm || 0), 0);
  totals.innerHTML = `
    <span>${history.length} prints</span>
    <span><strong>${(totalMm/1000).toFixed(1)} m</strong> total</span>
    <span><strong>${totalG.toFixed(0)} g</strong> total</span>
  `;

  tbody.innerHTML = history.map(e => {
    const date   = e.timestamp.replace("T", " ").slice(0, 16);
    const m      = (e.filament_mm / 1000).toFixed(2);
    const result = e.completed === false
      ? `<span style="color:var(--red);font-size:11px">cancelled</span>`
      : `<span style="color:var(--green);font-size:11px">done</span>`;
    return `<tr>
      <td class="col-date">${escHtml(date)}</td>
      <td>${escHtml(e.printer_name)}</td>
      <td class="col-file" title="${escAttr(e.filename)}">${escHtml(e.filename || "—")}</td>
      <td class="col-filament">${m} m · ${e.filament_g} g</td>
      <td>${formatTime(e.print_time_s)} ${result}</td>
    </tr>`;
  }).join("");
}

const historyPanel    = document.getElementById("panel-history");
const historyBackdrop = document.getElementById("panel-backdrop");
const btnHistory      = document.getElementById("btn-history");

const openHistory = () => {
  historyPanel.classList.add("open");
  historyBackdrop.classList.add("open");
  btnHistory.classList.add("active");
};
const closeHistory = () => {
  historyPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  btnHistory.classList.remove("active");
};

btnHistory.addEventListener("click", openHistory);
document.getElementById("btn-history-close").addEventListener("click", closeHistory);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeHistory(); closeSpools(); closePrinters(); closeFileBrowser(); } });

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
    const printerName = assignedTo ? (printers[assignedTo]?.name || assignedTo) : null;
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
    const otherPrinter = loc && loc !== printerId ? printers[loc] : null;
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
  try {
    // Unassign existing spool on this printer if it's different
    const cur = spools.find(s => spoolAssignedTo(s) === printerId);
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
        body: JSON.stringify({ location: printerId }),
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

// Spools side panel
const spoolsPanel = document.getElementById("panel-spools");
const btnSpools   = document.getElementById("btn-spools");

const openSpools = () => {
  spoolsPanel.classList.add("open");
  historyBackdrop.classList.add("open");
  btnSpools.classList.add("active");
  fetchSpools();
};
const closeSpools = () => {
  spoolsPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  btnSpools.classList.remove("active");
};

btnSpools.addEventListener("click", openSpools);
document.getElementById("btn-spools-close").addEventListener("click", closeSpools);

document.getElementById("btn-import-filaments").addEventListener("click", async () => {
  const btn = document.getElementById("btn-import-filaments");
  btn.disabled = true;
  btn.textContent = "Importing…";
  try {
    const r = await fetch("/api/import-filaments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brand: "ELEGOO" }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    if (d.created > 0) {
      alert(`✓ Imported ${d.created} ELEGOO filaments into Spoolman.\n\nYou can now create spools from these in Spoolman UI or when adding a spool here.`);
    } else {
      alert(`All ${d.total} ELEGOO filaments are already in Spoolman (${d.skipped} skipped).`);
    }
  } catch (e) {
    alert("Import failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
    </svg> Import Elegoo`;
  }
});

// Close all panels on backdrop click
historyBackdrop.addEventListener("click", () => { closeHistory(); closeSpools(); closePrinters(); });

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

// ─── Boot ──────────────────────────────────────────────────────────────────────
fetch("/api/auth-status")
  .then(r => r.json())
  .then(({ spoolman_url }) => {
    if (spoolman_url) document.getElementById("btn-spoolman-ui").href = spoolman_url;
  })
  .catch(() => {});
loadChangelog();
connect();
