/* Spooler – frontend app.js */
"use strict";

// ─── Elegoo Filament Catalog ───────────────────────────────────────────────────
const ELEGOO_CATALOG = [
  // PLA Standard
  { material: "PLA",            color: "Black",          hex: "#1a1a1a" },
  { material: "PLA",            color: "White",          hex: "#f0f0f0" },
  { material: "PLA",            color: "Grey",           hex: "#888888" },
  { material: "PLA",            color: "Space Grey",     hex: "#6b7280" },
  { material: "PLA",            color: "Red",            hex: "#dc2626" },
  { material: "PLA",            color: "Dark Blue",      hex: "#1e3a8a" },
  { material: "PLA",            color: "Sky Blue",       hex: "#38bdf8" },
  { material: "PLA",            color: "Yellow",         hex: "#eab308" },
  { material: "PLA",            color: "Orange",         hex: "#f97316" },
  { material: "PLA",            color: "Pink",           hex: "#f472b6" },
  { material: "PLA",            color: "Purple",         hex: "#7c3aed" },
  { material: "PLA",            color: "Neon Green",     hex: "#4ade80" },
  { material: "PLA",            color: "Sea Green",      hex: "#059669" },
  { material: "PLA",            color: "Translucent",    hex: "#cffafe" },
  { material: "PLA",            color: "Brown",          hex: "#7c4f2a" },
  { material: "PLA",            color: "Beige",          hex: "#d4b896" },
  { material: "PLA",            color: "Copper",         hex: "#b87333" },
  // PLA+
  { material: "PLA+",           color: "Black",          hex: "#1a1a1a" },
  { material: "PLA+",           color: "White",          hex: "#f0f0f0" },
  { material: "PLA+",           color: "Grey",           hex: "#888888" },
  { material: "PLA+",           color: "Space Grey",     hex: "#6b7280" },
  { material: "PLA+",           color: "Red",            hex: "#dc2626" },
  { material: "PLA+",           color: "Dark Blue",      hex: "#1e3a8a" },
  { material: "PLA+",           color: "Sky Blue",       hex: "#38bdf8" },
  { material: "PLA+",           color: "Yellow",         hex: "#eab308" },
  { material: "PLA+",           color: "Orange",         hex: "#f97316" },
  { material: "PLA+",           color: "Neon Green",     hex: "#4ade80" },
  { material: "PLA+",           color: "Sea Green",      hex: "#059669" },
  { material: "PLA+",           color: "Purple",         hex: "#7c3aed" },
  { material: "PLA+",           color: "Translucent Pink", hex: "#f9a8d4" },
  { material: "PLA+",           color: "Brown",          hex: "#7c4f2a" },
  { material: "PLA+",           color: "Beige",          hex: "#d4b896" },
  // PLA Rapid
  { material: "PLA Rapid",      color: "Black",          hex: "#1a1a1a" },
  { material: "PLA Rapid",      color: "White",          hex: "#f0f0f0" },
  { material: "PLA Rapid",      color: "Grey",           hex: "#888888" },
  { material: "PLA Rapid",      color: "Blue",           hex: "#2563eb" },
  { material: "PLA Rapid",      color: "Green",          hex: "#16a34a" },
  { material: "PLA Rapid",      color: "Red",            hex: "#dc2626" },
  { material: "PLA Rapid",      color: "Orange",         hex: "#f97316" },
  { material: "PLA Rapid",      color: "Yellow",         hex: "#eab308" },
  { material: "PLA Rapid",      color: "Silver",         hex: "#c0c0c0" },
  { material: "PLA Rapid",      color: "Brown",          hex: "#7c4f2a" },
  { material: "PLA Rapid",      color: "Beige",          hex: "#d4b896" },
  // PLA Silk
  { material: "PLA Silk",       color: "Gold",           hex: "#d4af37" },
  { material: "PLA Silk",       color: "White",          hex: "#f5f0e8" },
  { material: "PLA Silk",       color: "Coral Pink",     hex: "#f4826a" },
  { material: "PLA Silk",       color: "Mint Green",     hex: "#6ee7b7" },
  { material: "PLA Silk",       color: "Holly Green",    hex: "#166534" },
  { material: "PLA Silk",       color: "Blue Magenta",   hex: "#7c3aed" },
  { material: "PLA Silk",       color: "Green Red",      hex: "#16a34a" },
  { material: "PLA Silk",       color: "Black Red",      hex: "#7f1d1d" },
  { material: "PLA Silk",       color: "Black Purple",   hex: "#3b0764" },
  { material: "PLA Silk",       color: "Blue Green",     hex: "#0e7490" },
  { material: "PLA Silk",       color: "Blue Purple Black", hex: "#4c1d95" },
  { material: "PLA Silk",       color: "Blue Green Orange", hex: "#0891b2" },
  { material: "PLA Silk",       color: "Yellow Purple",  hex: "#a16207" },
  // PLA Matte
  { material: "PLA Matte",      color: "Black",          hex: "#1c1c1c" },
  { material: "PLA Matte",      color: "White",          hex: "#f0ede8" },
  { material: "PLA Matte",      color: "Navy Blue",      hex: "#1e3a5f" },
  { material: "PLA Matte",      color: "Ruby Red",       hex: "#9b111e" },
  { material: "PLA Matte",      color: "Teal Green",     hex: "#0d9488" },
  { material: "PLA Matte",      color: "Sunshine Yellow",hex: "#fbbf24" },
  { material: "PLA Matte",      color: "Slate Grey",     hex: "#708090" },
  { material: "PLA Matte",      color: "Lavender Purple",hex: "#a78bfa" },
  { material: "PLA Matte",      color: "Sakura Pink",    hex: "#f4a7b9" },
  { material: "PLA Matte",      color: "Ice Blue",       hex: "#bae6fd" },
  { material: "PLA Matte",      color: "Beige",          hex: "#d4b896" },
  { material: "PLA Matte",      color: "Mint Green",     hex: "#6ee7b7" },
  { material: "PLA Matte",      color: "Earth Brown",    hex: "#8b5e3c" },
  { material: "PLA Matte",      color: "Orange",         hex: "#f97316" },
  // PLA Glow
  { material: "PLA Glow",       color: "Blue",           hex: "#3b82f6" },
  { material: "PLA Glow",       color: "Green",          hex: "#22c55e" },
  { material: "PLA Glow",       color: "Yellow",         hex: "#fde047" },
  { material: "PLA Glow",       color: "Orange",         hex: "#fb923c" },
  { material: "PLA Glow",       color: "Pink",           hex: "#f472b6" },
  // PLA Sparkle
  { material: "PLA Sparkle",    color: "Black",          hex: "#1a1a1a" },
  { material: "PLA Sparkle",    color: "Gold",           hex: "#d4af37" },
  { material: "PLA Sparkle",    color: "Green",          hex: "#16a34a" },
  { material: "PLA Sparkle",    color: "Red",            hex: "#dc2626" },
  { material: "PLA Sparkle",    color: "Dark Grey",      hex: "#4b5563" },
  { material: "PLA Sparkle",    color: "Turquoise",      hex: "#06b6d4" },
  { material: "PLA Sparkle",    color: "Purplish Grey",  hex: "#7c7d9d" },
  // PLA Galaxy
  { material: "PLA Galaxy",     color: "Black",          hex: "#0f0f1a" },
  { material: "PLA Galaxy",     color: "Purple",         hex: "#4c1d95" },
  { material: "PLA Galaxy",     color: "Peacock Blue",   hex: "#005f73" },
  // PLA Marble
  { material: "PLA Marble",     color: "White",          hex: "#e8e8e8" },
  { material: "PLA Marble",     color: "Brick Red",      hex: "#8b3a3a" },
  { material: "PLA Marble",     color: "Cement Grey",    hex: "#a3a3a3" },
  // PLA Wood
  { material: "PLA Wood",       color: "Light Brown",    hex: "#c19a6b" },
  { material: "PLA Wood",       color: "Walnut",         hex: "#5c3317" },
  { material: "PLA Wood",       color: "Rosewood",       hex: "#65000b" },
  { material: "PLA Wood",       color: "Tan Birch",      hex: "#deb887" },
  { material: "PLA Wood",       color: "Medium Brown",   hex: "#8b6340" },
  // PLA-CF
  { material: "PLA-CF",         color: "Black",          hex: "#0d0d0d" },
  // PETG
  { material: "PETG",           color: "Black",          hex: "#1a1a1a" },
  { material: "PETG",           color: "White",          hex: "#f0f0f0" },
  { material: "PETG",           color: "Blue",           hex: "#2563eb" },
  { material: "PETG",           color: "Red",            hex: "#dc2626" },
  { material: "PETG",           color: "Green",          hex: "#16a34a" },
  { material: "PETG",           color: "Yellow",         hex: "#eab308" },
  { material: "PETG",           color: "Grey",           hex: "#888888" },
  { material: "PETG",           color: "Orange",         hex: "#f97316" },
  { material: "PETG",           color: "Brown",          hex: "#7c4f2a" },
  { material: "PETG",           color: "Beige",          hex: "#d4b896" },
  { material: "PETG",           color: "Space Grey",     hex: "#6b7280" },
  // PETG Pro
  { material: "PETG Pro",       color: "Black",          hex: "#1a1a1a" },
  { material: "PETG Pro",       color: "White",          hex: "#f0f0f0" },
  { material: "PETG Pro",       color: "Blue",           hex: "#2563eb" },
  { material: "PETG Pro",       color: "Grey",           hex: "#888888" },
  { material: "PETG Pro",       color: "Silver",         hex: "#c0c0c0" },
  { material: "PETG Pro",       color: "Pink",           hex: "#f472b6" },
  { material: "PETG Pro",       color: "Red",            hex: "#dc2626" },
  { material: "PETG Pro",       color: "Olive Green",    hex: "#4d7c0f" },
  { material: "PETG Pro",       color: "Light Blue",     hex: "#93c5fd" },
  { material: "PETG Pro",       color: "Green",          hex: "#16a34a" },
  { material: "PETG Pro",       color: "Yellow",         hex: "#eab308" },
  { material: "PETG Pro",       color: "Burgundy Red",   hex: "#7f1d1d" },
  { material: "PETG Pro",       color: "Purple",         hex: "#7c3aed" },
  // PETG Translucent
  { material: "PETG Translucent", color: "Blue",         hex: "#93c5fd" },
  { material: "PETG Translucent", color: "Green",        hex: "#86efac" },
  { material: "PETG Translucent", color: "Orange",       hex: "#fdba74" },
  { material: "PETG Translucent", color: "Purple",       hex: "#c4b5fd" },
  { material: "PETG Translucent", color: "Grey",         hex: "#d1d5db" },
  { material: "PETG Translucent", color: "Olive Green",  hex: "#bef264" },
  { material: "PETG Translucent", color: "Amber",        hex: "#f59e0b" },
  { material: "PETG Translucent", color: "Pink",         hex: "#f9a8d4" },
  // PETG-CF
  { material: "PETG-CF",        color: "Black",          hex: "#0d0d0d" },
  { material: "PETG-CF",        color: "Grey",           hex: "#555555" },
  { material: "PETG-CF",        color: "Green",          hex: "#14532d" },
  { material: "PETG-CF",        color: "Red",            hex: "#7f1d1d" },
  { material: "PETG-CF",        color: "Purple",         hex: "#3b0764" },
  { material: "PETG-CF",        color: "Blue",           hex: "#1e3a8a" },
  // PETG-GF
  { material: "PETG-GF",        color: "Black",          hex: "#1a1a1a" },
  { material: "PETG-GF",        color: "Grey",           hex: "#808080" },
  { material: "PETG-GF",        color: "White",          hex: "#f0f0f0" },
  // ABS
  { material: "ABS",            color: "Black",          hex: "#1a1a1a" },
  { material: "ABS",            color: "White",          hex: "#f0f0f0" },
  { material: "ABS",            color: "Red",            hex: "#dc2626" },
  { material: "ABS",            color: "Grey",           hex: "#888888" },
  { material: "ABS",            color: "Blue",           hex: "#2563eb" },
  { material: "ABS",            color: "Orange",         hex: "#f97316" },
  { material: "ABS",            color: "Yellow",         hex: "#eab308" },
  { material: "ABS",            color: "Green",          hex: "#16a34a" },
  { material: "ABS",            color: "Purple",         hex: "#7c3aed" },
  { material: "ABS",            color: "Pink",           hex: "#f472b6" },
  { material: "ABS",            color: "Cyan",           hex: "#06b6d4" },
  { material: "ABS",            color: "Olive Green",    hex: "#4d7c0f" },
  { material: "ABS",            color: "Sky Blue",       hex: "#38bdf8" },
  { material: "ABS",            color: "Navy Blue",      hex: "#1e3a5f" },
  // ASA
  { material: "ASA",            color: "Black",          hex: "#1a1a1a" },
  { material: "ASA",            color: "White",          hex: "#f0f0f0" },
  { material: "ASA",            color: "Blue",           hex: "#2563eb" },
  { material: "ASA",            color: "Green",          hex: "#16a34a" },
  { material: "ASA",            color: "Grey",           hex: "#888888" },
  { material: "ASA",            color: "Red",            hex: "#dc2626" },
  // TPU
  { material: "TPU 95A",        color: "Black",          hex: "#1a1a1a" },
  { material: "TPU 95A",        color: "White",          hex: "#f0f0f0" },
  { material: "TPU 95A",        color: "Translucent",    hex: "#e0f2fe" },
  { material: "TPU 95A",        color: "Red",            hex: "#dc2626" },
  { material: "TPU 95A",        color: "Blue",           hex: "#2563eb" },
  { material: "TPU 95A",        color: "Green",          hex: "#16a34a" },
  { material: "TPU 95A",        color: "Grey",           hex: "#888888" },
  // PC
  { material: "PC",             color: "Black",          hex: "#1a1a1a" },
  { material: "PC",             color: "White",          hex: "#f0f0f0" },
  { material: "PC",             color: "Transparent",    hex: "#e0f2fe" },
  // PC-FR
  { material: "PC-FR",          color: "Black",          hex: "#1a1a1a" },
  { material: "PC-FR",          color: "White",          hex: "#f0f0f0" },
  // PAHT-CF
  { material: "PAHT-CF",        color: "Black",          hex: "#0d0d0d" },
];

const WS_URL = `ws://${location.hostname}:8765`;
const RECONNECT_DELAY = 3000;

let ws = null;
let printers = {}; // id → printer data
let history  = []; // print history log
let spools   = {}; // id → spool data
let spoolPickerPrinterId = null;

// ─── WebSocket connection ──────────────────────────────────────────────────────
function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log("[WS] Connected");
    send({ action: "list_printers" });
    send({ action: "list_spools" });
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
    case "spools_list":
      spools = {};
      msg.spools.forEach(s => spools[s.id] = s);
      renderSpoolPanel();
      Object.values(printers).forEach(renderPrinter);
      break;
    case "spool_update":
      spools[msg.spool.id] = msg.spool;
      renderSpoolPanel();
      Object.values(printers).forEach(renderPrinter);
      break;
    case "spool_removed":
      delete spools[msg.spool_id];
      renderSpoolPanel();
      Object.values(printers).forEach(renderPrinter);
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

  const assignedSpool = Object.values(spools).find(s => s.assigned_to === printer.id) ?? null;

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
  const lightOn    = printer.status?.LightStatus?.SecondLight === 1;

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

    <!-- Spool -->
    <div class="card-spool" onclick="openSpoolPicker('${escAttr(printer.id)}')">
      ${assignedSpool ? (() => {
        const pct = assignedSpool.total_weight_g > 0
          ? Math.round((assignedSpool.remaining_g / assignedSpool.total_weight_g) * 100)
          : 0;
        const low = assignedSpool.remaining_g < 100;
        return `
          <span class="spool-dot" style="background:${escAttr(assignedSpool.color_hex)}"></span>
          <div class="spool-info">
            <div class="spool-name ${low ? "spool-low" : ""}">${escHtml(assignedSpool.name || assignedSpool.material)}</div>
            <div class="spool-bar-wrap">
              <div class="spool-bar-fill ${low ? "low" : ""}" style="width:${pct}%"></div>
            </div>
          </div>
          <span class="spool-remaining ${low ? "spool-low" : ""}">${assignedSpool.remaining_g.toFixed(0)} g</span>
        `;
      })() : `<span class="spool-assign-hint">+ Assign spool</span>`}
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
const openHistory  = () => {
  closeSpools();
  historyPanel.classList.add("open");
  historyBackdrop.classList.add("open");
  document.getElementById("btn-history").classList.add("active");
};
const closeHistory = () => {
  historyPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  document.getElementById("btn-history").classList.remove("active");
};

document.getElementById("btn-history").addEventListener("click", openHistory);
document.getElementById("btn-history-close").addEventListener("click", closeHistory);
historyBackdrop.addEventListener("click", () => { closeHistory(); closeSpools(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeHistory(); closeSpools(); } });

// ─── Spools panel ─────────────────────────────────────────────────────────────
const spoolsPanel    = document.getElementById("panel-spools");
const openSpools  = () => {
  closeHistory();
  spoolsPanel.classList.add("open");
  historyBackdrop.classList.add("open");
  document.getElementById("btn-spools").classList.add("active");
};
const closeSpools = () => {
  spoolsPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  document.getElementById("btn-spools").classList.remove("active");
};

document.getElementById("btn-spools").addEventListener("click", openSpools);
document.getElementById("btn-spools-close").addEventListener("click", closeSpools);

function renderSpoolPanel() {
  const list  = document.getElementById("spools-list");
  const empty = document.getElementById("spools-empty");
  const all   = Object.values(spools);

  if (!all.length) {
    list.innerHTML = "";
    list.appendChild(empty);
    empty.style.display = "";
    return;
  }
  empty.style.display = "none";

  const assignedPrinter = (spool) => {
    if (!spool.assigned_to) return null;
    return printers[spool.assigned_to] ?? null;
  };

  list.innerHTML = all.map(s => {
    const pct = s.total_weight_g > 0 ? Math.round((s.remaining_g / s.total_weight_g) * 100) : 0;
    const low = s.remaining_g < 100;
    const printer = assignedPrinter(s);
    return `
      <div class="spool-row">
        <div class="spool-row-color" style="background:${escAttr(s.color_hex)}"></div>
        <div class="spool-row-body">
          <div class="spool-row-header">
            <span class="spool-row-name">${escHtml(s.name || s.material)}</span>
            <span class="spool-row-meta">${escHtml(s.brand ? s.brand + " · " : "")}${escHtml(s.material)}</span>
          </div>
          <div class="spool-bar-wrap spool-bar-large">
            <div class="spool-bar-fill ${low ? "low" : ""}" style="width:${pct}%"></div>
          </div>
          <div class="spool-row-stats">
            <span class="${low ? "spool-low" : ""}">${s.remaining_g.toFixed(0)} g remaining</span>
            <span style="color:var(--text-muted)">/ ${s.total_weight_g.toFixed(0)} g total</span>
            ${printer ? `<span class="spool-assigned-tag">${escHtml(printer.name)}</span>` : ""}
          </div>
        </div>
        <div class="spool-row-actions">
          <button class="btn btn-secondary btn-sm" onclick="openEditSpool('${escAttr(s.id)}')">Edit</button>
          <button class="btn btn-danger btn-sm" onclick="deleteSpool('${escAttr(s.id)}')">Delete</button>
        </div>
      </div>
    `;
  }).join("");
}

// ─── Add / Edit Spool Modal ────────────────────────────────────────────────────
const spoolModal = document.getElementById("modal-spool");
const openSpoolModal  = () => spoolModal.classList.add("open");

// ─── Elegoo Catalog Picker ─────────────────────────────────────────────────────
const catalogModal  = document.getElementById("modal-catalog");
const catalogFilter = document.getElementById("catalog-filter");
const catalogList   = document.getElementById("catalog-list");

function renderCatalogList(q) {
  const needle = (q || "").trim().toLowerCase();
  const items  = needle
    ? ELEGOO_CATALOG.filter(e => (e.material + " " + e.color).toLowerCase().includes(needle))
    : ELEGOO_CATALOG;

  catalogList.innerHTML = "";
  items.forEach(e => {
    const row = document.createElement("div");
    row.className = "picker-row";
    row.innerHTML = `
      <span class="spool-dot" style="background:${escAttr(e.hex)}"></span>
      <div class="picker-row-body">
        <div class="picker-row-name">${escHtml(e.material)} ${escHtml(e.color)}</div>
      </div>
    `;
    row.addEventListener("click", () => {
      fillFromCatalog(e);
      catalogModal.classList.remove("open");
      catalogFilter.value = "";
    });
    catalogList.appendChild(row);
  });
}

catalogFilter.addEventListener("input", () => renderCatalogList(catalogFilter.value));

document.getElementById("btn-browse-catalog").addEventListener("click", () => {
  catalogFilter.value = "";
  renderCatalogList("");
  catalogModal.classList.add("open");
  setTimeout(() => catalogFilter.focus(), 50);
});

document.getElementById("btn-catalog-close").addEventListener("click", () => {
  catalogModal.classList.remove("open");
});
catalogModal.addEventListener("click", (e) => {
  if (e.target === catalogModal) catalogModal.classList.remove("open");
});

function fillFromCatalog(entry) {
  document.getElementById("spool-name").value       = `Elegoo ${entry.material} ${entry.color}`;
  document.getElementById("spool-brand").value      = "Elegoo";
  document.getElementById("spool-color-name").value = entry.color;
  document.getElementById("spool-color-hex").value  = entry.hex;
  const sel = document.getElementById("spool-material");
  const opt = [...sel.options].find(o => o.value === entry.material);
  if (opt) sel.value = entry.material;
}

document.getElementById("btn-spool-new").addEventListener("click", () => {
  document.getElementById("modal-spool-title").textContent = "Add Spool";
  document.getElementById("spool-edit-id").value = "";
  document.getElementById("spool-name").value = "";
  document.getElementById("spool-brand").value = "";
  document.getElementById("spool-material").value = "PLA";
  document.getElementById("spool-color-name").value = "";
  document.getElementById("spool-color-hex").value = "#888888";
  document.getElementById("spool-total-g").value = "1000";
  document.getElementById("spool-remaining-g").value = "1000";
  openSpoolModal();
});

function openEditSpool(id) {
  const s = spools[id];
  if (!s) return;
  document.getElementById("modal-spool-title").textContent = "Edit Spool";
  document.getElementById("spool-edit-id").value = s.id;
  document.getElementById("spool-name").value = s.name;
  document.getElementById("spool-brand").value = s.brand;
  document.getElementById("spool-material").value = s.material;
  document.getElementById("spool-color-name").value = s.color_name;
  document.getElementById("spool-color-hex").value = s.color_hex;
  document.getElementById("spool-total-g").value = s.total_weight_g;
  document.getElementById("spool-remaining-g").value = s.remaining_g;
  openSpoolModal();
}

const closeSpoolModal = () => spoolModal.classList.remove("open");
document.getElementById("btn-spool-modal-cancel").addEventListener("click", closeSpoolModal);
spoolModal.addEventListener("click", (e) => { if (e.target === spoolModal) closeSpoolModal(); });

document.getElementById("btn-spool-modal-save").addEventListener("click", () => {
  const editId   = document.getElementById("spool-edit-id").value;
  const totalG   = parseFloat(document.getElementById("spool-total-g").value) || 1000;
  const remainG  = parseFloat(document.getElementById("spool-remaining-g").value);
  const material  = document.getElementById("spool-material").value;
  const colorName = document.getElementById("spool-color-name").value.trim();
  const nameRaw   = document.getElementById("spool-name").value.trim();
  const payload  = {
    name:           nameRaw || [material, colorName].filter(Boolean).join(" ") || "Ny spole",
    brand:          document.getElementById("spool-brand").value.trim(),
    material,
    color_name:     colorName,
    color_hex:      document.getElementById("spool-color-hex").value,
    total_weight_g: totalG,
    remaining_g:    isNaN(remainG) ? totalG : remainG,
  };
  if (editId) {
    send({ action: "update_spool", spool_id: editId, ...payload });
  } else {
    send({ action: "add_spool", ...payload });
  }
  closeSpoolModal();
});

function deleteSpool(id) {
  if (confirm("Delete this spool?")) {
    send({ action: "delete_spool", spool_id: id });
  }
}

// ─── Spool Picker (assign to printer) ─────────────────────────────────────────
const spoolPickerModal = document.getElementById("modal-spool-picker");

function openSpoolPicker(printerId) {
  spoolPickerPrinterId = printerId;
  const list = document.getElementById("spool-picker-list");
  const all  = Object.values(spools);

  const noneRow = document.createElement("div");
  noneRow.className = "picker-row";
  noneRow.textContent = "— No spool —";
  noneRow.onclick = () => { send({ action: "assign_spool", printer_id: printerId, spool_id: null }); spoolPickerModal.classList.remove("open"); };
  list.innerHTML = "";
  list.appendChild(noneRow);

  if (!all.length) {
    const hint = document.createElement("p");
    hint.style.cssText = "color:var(--text-muted);font-size:13px;padding:12px 0";
    hint.textContent = "No spools added yet. Use the Spools panel to add one.";
    list.appendChild(hint);
  }

  all.forEach(s => {
    const pct = s.total_weight_g > 0 ? Math.round((s.remaining_g / s.total_weight_g) * 100) : 0;
    const active = s.assigned_to === printerId;
    const row = document.createElement("div");
    row.className = "picker-row" + (active ? " active" : "");
    row.innerHTML = `
      <span class="spool-dot" style="background:${escAttr(s.color_hex)}"></span>
      <div class="picker-row-body">
        <div class="picker-row-name">${escHtml(s.name || s.material)}</div>
        <div class="picker-row-sub">${escHtml(s.material)}${s.brand ? " · " + escHtml(s.brand) : ""} · ${s.remaining_g.toFixed(0)} g (${pct}%)</div>
      </div>
      ${active ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:16px;height:16px;color:var(--green);flex-shrink:0"><polyline points="20 6 9 17 4 12"/></svg>` : ""}
    `;
    row.onclick = () => {
      send({ action: "assign_spool", printer_id: printerId, spool_id: s.id });
      spoolPickerModal.classList.remove("open");
    };
    list.appendChild(row);
  });

  spoolPickerModal.classList.add("open");
}

document.getElementById("btn-picker-cancel").addEventListener("click", () => {
  spoolPickerModal.classList.remove("open");
});
spoolPickerModal.addEventListener("click", (e) => { if (e.target === spoolPickerModal) spoolPickerModal.classList.remove("open"); });

// ─── Boot ──────────────────────────────────────────────────────────────────────
connect();
