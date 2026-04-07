/* Spooler – frontend app.js */
"use strict";

const WS_URL = `ws://${location.hostname}:8765`;
const RECONNECT_DELAY = 3000;

let ws = null;
let printers = {}; // id → printer data
let history  = []; // print history log

// ─── WebSocket connection ──────────────────────────────────────────────────────
function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log("[WS] Connected");
    send({ action: "list_printers" });
    loadHistory();
  };

  ws.onmessage = (ev) => {
    try {
      handleMessage(JSON.parse(ev.data));
    } catch (e) {
      console.error("[WS] Bad message", e);
    }
  };

  ws.onclose = () => {
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

function isPaused(printer) {
  const s = getPrintStatus(printer);
  return ["paused", "pausing"].includes(s);
}

function getProgress(printer) {
  const pi = printer.status?.PrintInfo;
  if (!pi) return null;
  const cur = pi.CurrentLayer ?? pi.CurrentTicks;
  const tot = pi.TotalLayer   ?? pi.TotalTicks;
  if (!tot) return null;
  return Math.round((cur / tot) * 100);
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

  const cameraUrl = printer.camera_url;

  card.innerHTML = `
    <!-- Header -->
    <div class="card-header">
      <div class="status-dot ${sc}"></div>
      <div class="card-header-info">
        <div class="card-title">${escHtml(printer.name)}</div>
        <div class="card-subtitle">${escHtml(printer.ip)}${printer.attrs?.FirmwareVersion ? ` · fw ${escHtml(printer.attrs.FirmwareVersion)}` : ""}</div>
      </div>
      <span class="status-badge ${sc}">${status}</span>
      <button class="card-remove-btn" onclick="removePrinter('${escAttr(printer.id)}')" title="Remove printer">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>
        </svg>
      </button>
    </div>

    <!-- Camera -->
    <div class="card-camera">
      ${cameraUrl
        ? `<img src="${escAttr(cameraUrl)}" alt="Camera feed" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" />`
        : ""}
      <div class="camera-placeholder" style="display:${cameraUrl ? "none" : "flex"}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
          <path d="M23 7l-7 5 7 5V7z"/>
          <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
        </svg>
        <span>${connected ? "No camera feed" : "Printer offline"}</span>
      </div>
    </div>

    <!-- Progress (only when printing/paused) -->
    ${(printing || paused || progress !== null) ? `
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
      <button class="btn btn-secondary btn-sm" onclick="printerAction('${escAttr(printer.id)}','light_on')" title="Light on" ${!connected ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="5"/>
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
        </svg>
      </button>
      <button class="btn btn-secondary btn-sm" onclick="printerAction('${escAttr(printer.id)}','light_off')" title="Light off" ${!connected ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="2" y1="2" x2="22" y2="22"/>
          <path d="M9 9a3 3 0 0 0 5.12 2.12M10.72 5.08A6 6 0 0 1 18.99 12c0 1.72-.7 3.29-1.82 4.44"/>
          <path d="M6.71 6.71A6 6 0 0 0 5 12c0 2.74 1.85 5.06 4.36 5.77"/>
          <path d="M2 15h2M2 9h2M12 21v-2"/>
        </svg>
      </button>
      <button class="btn btn-secondary btn-sm" onclick="printerAction('${escAttr(printer.id)}','camera_on')" title="Refresh camera" ${!connected ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M23 7l-7 5 7 5V7z"/>
          <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
        </svg>
      </button>
    </div>
  `;
}

function syncEmptyState() {
  const empty = document.getElementById("empty-state");
  const grid  = document.getElementById("printer-grid");
  const has   = Object.keys(printers).length > 0;
  empty.style.display = has ? "none" : "";
  grid.style.display  = has ? ""     : "none";
}

// ─── User actions ──────────────────────────────────────────────────────────────
function printerAction(id, action) {
  send({ action, printer_id: id });
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

// ─── Discover / Add modal ──────────────────────────────────────────────────────
document.getElementById("btn-discover").addEventListener("click", () => {
  send({ action: "discover" });
  toast("Scanning network for printers…");
});

const modal = document.getElementById("modal-add");
const openModal  = () => modal.classList.add("open");
const closeModal = () => modal.classList.remove("open");

document.getElementById("btn-add").addEventListener("click", openModal);
document.getElementById("btn-modal-cancel").addEventListener("click", closeModal);
document.getElementById("btn-modal-confirm").addEventListener("click", () => {
  const ip   = document.getElementById("input-ip").value.trim();
  const name = document.getElementById("input-name").value.trim();
  if (!ip) { toast("Enter an IP address", true); return; }
  send({ action: "add_printer", ip, name: name || undefined });
  closeModal();
  document.getElementById("input-ip").value = "";
  document.getElementById("input-name").value = "";
});

// Close modal on backdrop click or Escape key
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

// ─── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const area = document.getElementById("toast-area");
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(() => el.remove(), 3500);
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
const openHistory  = () => { historyPanel.classList.add("open"); historyBackdrop.classList.add("open"); };
const closeHistory = () => { historyPanel.classList.remove("open"); historyBackdrop.classList.remove("open"); };

document.getElementById("btn-history").addEventListener("click", openHistory);
document.getElementById("btn-history-close").addEventListener("click", closeHistory);
historyBackdrop.addEventListener("click", closeHistory);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHistory(); });

// ─── Boot ──────────────────────────────────────────────────────────────────────
connect();
