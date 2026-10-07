/* Spooler frontend: Printer cards and user actions. Plain scripts sharing one global scope; load order is in index.html. */
"use strict";

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

  // An offline printer's last temperatures are history, not a reading: show "--".
  const nozzle     = !connected ? 0 : printer.status?.TempOfNozzle    ?? printer.status?.NozzleTemp    ?? 0;
  const nozzleTgt  = !connected ? 0 : printer.status?.TempTargetNozzle?? printer.status?.NozzleTempTarget ?? 0;
  const bed        = !connected ? 0 : printer.status?.TempOfHotbed     ?? printer.status?.BedTemp       ?? 0;
  const bedTgt     = !connected ? 0 : printer.status?.TempTargetHotbed ?? printer.status?.BedTempTarget ?? 0;
  const chamber    = !connected ? 0 : printer.status?.TempOfBox        ?? printer.status?.ChamberTemp   ?? 0;
  const chamberTgt = !connected ? 0 : printer.status?.TempTargetBox    ?? 0;

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

