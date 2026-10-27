// Advanced control panel (T12): temperatures, fans, axes. The server decides
// what is allowed (control.py); this file only lays it out.

"use strict";

const CTL_UNLOCK_MS = 10 * 60 * 1000;
const _ctlUnlocked = {};   // printerId → last activity (ms)
let _ctlPrinterId = null;
let _ctlStep = 1;
let _ctlTimer = null;

const CTL_PRESETS = [
  { name: "PLA",  nozzle: 210, bed: 60 },
  { name: "PETG", nozzle: 240, bed: 80 },
  { name: "ABS",  nozzle: 260, bed: 100 },
];

const _ICON_WRENCH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>`;

function controlAvailable(printer) {
  return !!(features.advanced_control && features.advanced_control.enabled)
    && (printer.capabilities || []).length > 0;
}

function controlButtonHtml(printer) {
  if (!controlAvailable(printer)) return "";
  return `<button class="btn btn-sm btn-secondary" title="Advanced control" aria-label="Advanced control"
            onclick="openControl('${escAttr(printer.id)}')" ${!printer.connected ? "disabled" : ""}>${_ICON_WRENCH}</button>`;
}

function _ctlStatus(p) {
  const s = p.status || {};
  return {
    nozzle:  { cur: s.TempOfNozzle  ?? s.NozzleTemp ?? 0, tgt: s.TempTargetNozzle ?? s.NozzleTempTarget ?? 0 },
    bed:     { cur: s.TempOfHotbed  ?? s.BedTemp    ?? 0, tgt: s.TempTargetHotbed ?? s.BedTempTarget    ?? 0 },
    chamber: { cur: s.TempOfBox     ?? s.ChamberTemp ?? 0, tgt: s.TempTargetBox ?? 0 },
  };
}

function openControl(id) {
  const p = printers[id];
  if (!p || !controlAvailable(p)) return;
  const last = _ctlUnlocked[id];
  if (!last || Date.now() - last > CTL_UNLOCK_MS) {
    if (!confirm(`You are about to control ${p.name || "this printer"} directly.\nChanges happen immediately.`)) return;
  }
  _ctlUnlocked[id] = Date.now();
  _ctlPrinterId = id;
  let m = document.getElementById("modal-control");
  if (!m) {
    m = document.createElement("div");
    m.id = "modal-control";
    m.className = "modal";
    m.addEventListener("click", e => { if (e.target === m) closeControl(); });
    document.body.appendChild(m);
  }
  m.innerHTML = _controlHtml(p);
  m.classList.add("open");
  clearInterval(_ctlTimer);
  _ctlTimer = setInterval(_controlRefresh, 1000);
}

function closeControl() {
  clearInterval(_ctlTimer);
  _ctlPrinterId = null;
  document.getElementById("modal-control")?.classList.remove("open");
}

function _ctlSend(action, extra) {
  if (!_ctlPrinterId) return;
  _ctlUnlocked[_ctlPrinterId] = Date.now();
  send({ action, printer_id: _ctlPrinterId, ...extra });
}

function ctlSetTemp(heater) {
  const el = document.getElementById("ctl-in-" + heater);
  const v = parseFloat(el.value);
  if (isNaN(v)) return;
  _ctlSend("set_temperature", { heater, target: v });
}
function ctlHeaterOff(heater) { _ctlSend("set_temperature", { heater, target: 0 }); }
function ctlPreset(i) {
  const pr = CTL_PRESETS[i], p = printers[_ctlPrinterId];
  if (!p) return;
  const caps = p.capabilities || [];
  if (caps.includes("temp_nozzle")) _ctlSend("set_temperature", { heater: "nozzle", target: pr.nozzle });
  if (caps.includes("temp_bed")) _ctlSend("set_temperature", { heater: "bed", target: pr.bed });
}
function ctlSetFan(fan, pct) { _ctlSend("set_fan", { fan, percent: +pct }); }
function ctlMove(axis, sign) { _ctlSend("move_axis", { axis, distance: sign * _ctlStep }); }
function ctlHome(axes) { _ctlSend("home", { axes }); }
function ctlLevel() {
  if (confirm("Start auto leveling? The printer will heat up and probe the bed.")) _ctlSend("auto_level", {});
}
function ctlAllOff() { _ctlSend("heaters_off", {}); }
function ctlStep(v) {
  _ctlStep = v;
  document.querySelectorAll("#modal-control .ctl-step").forEach(b => b.classList.toggle("active", +b.dataset.step === v));
}

function _controlRefresh() {
  const p = printers[_ctlPrinterId];
  if (!p) return;
  const t = _ctlStatus(p);
  for (const h of ["nozzle", "bed", "chamber"]) {
    const c = document.getElementById("ctl-cur-" + h), g = document.getElementById("ctl-tgt-" + h);
    if (c) c.textContent = Math.round(t[h].cur);
    if (g) g.textContent = t[h].tgt ? Math.round(t[h].tgt) + "°" : "off";
  }
  const busy = ["printing", "pausing", "paused", "preparing", "stopping"].includes(p.state);
  document.getElementById("ctl-motion")?.classList.toggle("ctl-disabled", busy);
  document.getElementById("ctl-motion-note")?.classList.toggle("hidden", !busy);
}

function _controlHtml(p) {
  const caps = p.capabilities || [], lim = p.limits || {}, t = _ctlStatus(p);
  const has = c => caps.includes(c);
  const heaters = [["nozzle", "Nozzle", "temp_nozzle"], ["bed", "Bed", "temp_bed"], ["chamber", "Chamber", "temp_chamber"]]
    .filter(([, , c]) => has(c));
  const heaterRow = ([h, label]) => {
    const max = (lim[h] || {}).max ?? 300;
    return `<div class="ctl-heater">
      <div class="ctl-heater-name">${label}</div>
      <div class="ctl-temp"><span id="ctl-cur-${h}">${Math.round(t[h].cur)}</span><small>°C</small>
        <span class="ctl-target">→ <span id="ctl-tgt-${h}">${t[h].tgt ? Math.round(t[h].tgt) + "°" : "off"}</span></span></div>
      <div class="ctl-heater-set">
        <input type="number" id="ctl-in-${h}" min="0" max="${max}" step="5" value="${t[h].tgt || ""}" placeholder="0–${max}" aria-label="${label} target">
        <button class="btn btn-sm btn-primary" onclick="ctlSetTemp('${h}')">Set</button>
        <button class="btn btn-sm btn-secondary" onclick="ctlHeaterOff('${h}')">Off</button>
      </div></div>`;
  };
  const fans = [["part", "Part cooling", "fan_part"], ["aux", "Auxiliary", "fan_aux"], ["chamber", "Chamber", "fan_chamber"]]
    .filter(([, , c]) => has(c));
  const fanRow = ([f, label]) => `<div class="ctl-fan"><span>${label}</span>
      <input type="range" min="0" max="100" step="5" value="0" aria-label="${label} speed"
             oninput="this.nextElementSibling.textContent=this.value+'%'" onchange="ctlSetFan('${f}',this.value)">
      <output>0%</output></div>`;
  const jog = (a, s, txt, cls = "") => `<button class="ctl-jog ${cls}" onclick="ctlMove('${a}',${s})" aria-label="${a}${s > 0 ? "+" : "−"}">${txt}</button>`;
  const motion = (has("move") || has("home") || has("level")) ? `
    <section class="ctl-section">
      <h4>Motion</h4>
      <p id="ctl-motion-note" class="ctl-note hidden">Axes are locked while the printer is printing or paused.</p>
      <div id="ctl-motion" class="ctl-motion">
        <div class="ctl-pad">
          <span></span>${jog("Y", 1, "Y+")}<span></span>
          ${jog("X", -1, "X−")}<button class="ctl-jog ctl-home" onclick="ctlHome('XY')" aria-label="Home XY">⌂</button>${jog("X", 1, "X+")}
          <span></span>${jog("Y", -1, "Y−")}<span></span>
        </div>
        <div class="ctl-zcol">${jog("Z", 1, "Z+")}<button class="ctl-jog ctl-home" onclick="ctlHome('Z')" aria-label="Home Z">⌂</button>${jog("Z", -1, "Z−")}</div>
        <div class="ctl-side">
          <div class="ctl-steps" role="group" aria-label="Step size">
            ${[0.1, 1, 10].map(v => `<button class="ctl-step${v === _ctlStep ? " active" : ""}" data-step="${v}" onclick="ctlStep(${v})">${v} mm</button>`).join("")}
          </div>
          <div class="ctl-btnrow">
            <button class="btn btn-sm btn-secondary" onclick="ctlHome('XYZ')">Home all</button>
            ${has("level") ? `<button class="btn btn-sm btn-secondary" onclick="ctlLevel()">Auto leveling</button>` : ""}
          </div>
        </div>
      </div>
    </section>` : "";
  return `<div class="modal-box ctl-box" role="dialog" aria-label="Advanced control">
    <div class="ctl-head">
      <h3>${_ICON_WRENCH}<span>${escHtml(p.name || "Printer")}</span><em>Advanced control</em></h3>
      <button class="btn btn-sm btn-secondary" onclick="closeControl()" aria-label="Close">✕</button>
    </div>
    <div class="ctl-warn">You control the printer directly. Changes happen immediately.</div>
    ${p.controls_verified ? "" : `<div class="ctl-preview">Preview: these commands are not yet verified on a real printer, so they are not sent. Buttons will answer “Not supported by this printer yet”.</div>`}
    ${heaters.length ? `<section class="ctl-section"><h4>Temperature</h4>
      <div class="ctl-presets">${CTL_PRESETS.map((pr, i) => `<button class="ctl-chip" onclick="ctlPreset(${i})">${pr.name} <small>${pr.nozzle}/${pr.bed}</small></button>`).join("")}</div>
      ${heaters.map(heaterRow).join("")}</section>` : ""}
    ${fans.length ? `<section class="ctl-section"><h4>Fans</h4>${fans.map(fanRow).join("")}</section>` : ""}
    ${motion}
    <button class="btn btn-danger ctl-alloff" onclick="ctlAllOff()">Turn off all heating</button>
  </div>`;
}
