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
const CTL_FAN_STEPS = [0, 25, 50, 75, 100];

const _ICON_WRENCH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>`;

function controlAvailable(printer) {
  return !!(features.advanced_control && features.advanced_control.enabled)
    && (window.SPOOLER_PERMS || []).includes("printer.advanced")
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
  const m = _ctlModal();
  const last = _ctlUnlocked[id];
  if (!_ctlWarnOff() && (!last || Date.now() - last > CTL_UNLOCK_MS)) {
    // Spooler's own warning instead of the browser's confirm(); "Don't show again" is per browser.
    clearInterval(_ctlTimer);
    _ctlPrinterId = null;
    m.innerHTML = `<div class="modal-box ctl-box ctl-warnbox" role="alertdialog" aria-labelledby="ctl-warn-title">
      <div class="ctl-warn-icon">${_ICON_WRENCH}</div>
      <h3 id="ctl-warn-title">Advanced control</h3>
      <p>You are about to control <strong>${escHtml(p.name || "this printer")}</strong> directly. Temperatures, fans,
        movement and calibration are sent to the printer right away, without the checks a print job has.</p>
      <p>Make sure the printer is free to move and that nobody is working on it.</p>
      <label class="ctl-warn-skip"><input type="checkbox" id="ctl-warn-skip"> Don't show this again</label>
      <div class="ctl-warn-btns">
        <button class="btn btn-secondary" onclick="closeControl()">Cancel</button>
        <button class="btn btn-primary" id="ctl-warn-ok" onclick="_ctlWarnAccept('${escAttr(id)}')">Continue</button>
      </div>
    </div>`;
    m.classList.add("open");
    document.getElementById("ctl-warn-ok")?.focus();
    return;
  }
  _ctlUnlocked[id] = Date.now();
  _ctlPrinterId = id;
  m.innerHTML = _controlHtml(p);
  m.classList.add("open");
  _ctlFanRefresh(p);
  _ctlIdle = null;
  ctlLoadIdle();
  if (_ctlTab === "log") ctlLoadLog();
  clearInterval(_ctlTimer);
  _ctlTimer = setInterval(_controlRefresh, 1000);
}

const CTL_WARN_KEY = "spooler.ctlWarnOff";
function _ctlWarnOff() {
  try { return localStorage.getItem(CTL_WARN_KEY) === "1"; } catch (_) { return false; }
}
function _ctlWarnAccept(id) {
  if (document.getElementById("ctl-warn-skip")?.checked) {
    try { localStorage.setItem(CTL_WARN_KEY, "1"); } catch (_) {}
  }
  _ctlUnlocked[id] = Date.now();
  openControl(id);
}
function _ctlModal() {
  let m = document.getElementById("modal-control");
  if (!m) {
    m = document.createElement("div");
    m.id = "modal-control";
    m.className = "modal";
    m.addEventListener("click", e => { if (e.target === m) closeControl(); });
    document.body.appendChild(m);
  }
  return m;
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
function ctlSetFan(fan, pct) {
  if (pct === "" || pct == null) return;              // empty field: ignore, use Off to stop
  const v = Math.round(Math.max(0, Math.min(100, Number(pct))));
  if (!Number.isFinite(v)) return;
  _ctlSend("set_fan", { fan, percent: v });
  // reflect the sent value in both controls right away
  const r = document.getElementById("ctl-fan-range-" + fan), n = document.getElementById("ctl-fan-in-" + fan);
  if (r) r.value = v;
  if (n) n.value = v;
}
// Live-sync the number field while the slider is dragged (does not send).
function ctlFanSlide(fan, val) {
  const n = document.getElementById("ctl-fan-in-" + fan);
  if (n) n.value = val;
}
function ctlFanOff(fan) { ctlSetFan(fan, 0); }
function ctlMove(axis, sign) { _ctlSend("move_axis", { axis, distance: sign * _ctlStep }); }
function ctlHome(axes) { _ctlSend("home", { axes }); }
function ctlLevel() {
  if (confirm("Start auto leveling? The printer will heat up and probe the bed.")) _ctlSend("auto_level", {});
}
function ctlAllOff() { _ctlSend("heaters_off", {}); }
// PID: the button opens a small chooser for the target; Start runs it.
function ctlPidOpen(show = true) {
  const box = document.getElementById("ctl-pid-box");
  if (box) box.hidden = !show;
  if (show) document.getElementById("ctl-pid-target")?.focus();
}
function ctlPid() {
  const t = Math.round(Number(document.getElementById("ctl-pid-target")?.value || 220));
  if (!Number.isFinite(t) || t < 150 || t > 280) { toast("PID target must be 150–280 °C", true); return; }
  _ctlSend("pid_calibrate", { target: t });
  ctlPidOpen(false);
}
function ctlShaper() {
  if (confirm("Run the resonance test (input shaper)? The printer homes and shakes the toolhead and bed for a few minutes. Keep the bed clear."))
    _ctlSend("shaper_calibrate", {});
}
function ctlStep(v) {
  _ctlStep = v;
  document.querySelectorAll("#modal-control .ctl-step").forEach(b => b.classList.toggle("active", +b.dataset.step === v));
}

// Show the speed the printer reports, and move the slider/field to match it --
// but never while the user is dragging or typing (don't fight their input).
function _ctlFanRefresh(p) {
  const speeds = (p.status || {}).FanSpeed || {};
  for (const [f, cur] of Object.entries(speeds)) {
    const o = document.getElementById("ctl-fan-" + f);
    if (o) o.textContent = cur + "%";
    const r = document.getElementById("ctl-fan-range-" + f);
    const n = document.getElementById("ctl-fan-in-" + f);
    if (r && document.activeElement !== r) r.value = cur;
    if (n && document.activeElement !== n) n.value = cur;
  }
}

function _controlRefresh() {
  const p = printers[_ctlPrinterId];
  if (!p) return;
  const t = _ctlStatus(p);
  for (const h of ["nozzle", "bed", "chamber"]) {
    const c = document.getElementById("ctl-cur-" + h), g = document.getElementById("ctl-tgt-" + h);
    if (c) c.textContent = Math.round(t[h].cur);
    if (g) g.textContent = t[h].tgt ? Math.round(t[h].tgt) + "°" : "off";
    document.getElementById("ctl-tile-" + h)?.classList.toggle("on", !!t[h].tgt);
  }
  const st = document.getElementById("ctl-state");
  if (st) { st.textContent = _ctlStateLabel(p.state); st.dataset.state = p.state || ""; }
  _ctlFanRefresh(p);
  const busy = ["printing", "pausing", "paused", "preparing", "stopping"].includes(p.state);
  document.getElementById("ctl-motion")?.classList.toggle("ctl-disabled", busy);
  document.getElementById("ctl-motion-note")?.classList.toggle("hidden", !busy);
  if (_ctlIdle && Date.now() - _ctlIdle.at > 15000) ctlLoadIdle();
  else _ctlIdleRender();
}

function _ctlStateLabel(s) {
  return { idle: "Idle", printing: "Printing", paused: "Paused", pausing: "Pausing", preparing: "Busy",
           stopping: "Stopping", complete: "Done", cancelled: "Cancelled", error: "Error", offline: "Offline" }[s] || "Idle";
}

// Tabs: the last one used stays selected while the page is open.
let _ctlTab = "heat";
function ctlTab(name) {
  _ctlTab = name;
  document.querySelectorAll("#modal-control .ctl-tab").forEach(b => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on);
  });
  document.querySelectorAll("#modal-control .ctl-pane").forEach(el => { el.hidden = el.dataset.pane !== name; });
  if (name === "log") ctlLoadLog();
}

// Idle guard: heaters left on without a print turn off after N minutes (0 = off).
let _ctlIdle = null;
async function ctlLoadIdle() {
  try {
    const r = await fetch("/api/control/idle-guard");
    if (!r.ok) return;
    _ctlIdle = { ...(await r.json()), at: Date.now() };
  } catch (_) { return; }
  _ctlIdleRender();
}
function _ctlIdleRender() {
  if (!_ctlIdle) return;
  const edit = document.getElementById("ctl-idle");
  if (edit && !edit.dataset.built) {
    edit.dataset.built = "1";
    edit.innerHTML = _ctlIdle.can_edit
      ? `<input type="number" id="ctl-idle-min" min="0" max="1440" step="5" value="${_ctlIdle.minutes}"
           aria-label="Idle guard minutes" onkeydown="if (event.key === 'Enter') ctlSaveIdle()"><span>min</span>
         <button class="btn btn-sm btn-secondary" onclick="ctlSaveIdle()">Save</button>`
      : `<span class="ctl-idle-val">${_ctlIdle.minutes ? _ctlIdle.minutes + " min" : "Off"}</span>`;
  }
  const left = _ctlIdle.printers?.[_ctlPrinterId];
  const text = document.getElementById("ctl-idle-text");
  if (!text) return;
  if (_ctlIdle.minutes && left != null) {
    const s = Math.max(0, left - Math.floor((Date.now() - _ctlIdle.at) / 1000));
    text.textContent = `Not printing · heating turns off in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    text.hidden = false;
  } else text.hidden = true;
}
async function ctlSaveIdle() {
  const min = Number(document.getElementById("ctl-idle-min")?.value);
  if (!Number.isInteger(min) || min < 0 || min > 1440) { toast("Enter whole minutes, 0–1440 (0 = off)", true); return; }
  try {
    const r = await fetch("/api/integrations", { method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: { "control.idle_heater_min": min } }) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { toast(data.error || "Could not save", true); return; }
    toast(min ? `Idle guard: heating turns off after ${min} min` : "Idle guard turned off");
    ctlLoadIdle();
  } catch (e) { toast("Could not save: " + e.message, true); }
}

// Who did what on this printer from the advanced panel, newest first.
const _CTL_ACTION_LABEL = { set_temperature: "Temperature", set_fan: "Fan", move_axis: "Move", home: "Home",
                            auto_level: "Auto leveling", heaters_off: "All heating off",
                            pid_calibrate: "PID calibration", shaper_calibrate: "Resonance test" };
function _ctlParamText(e) {
  const p = e.params || {};
  if (e.action === "set_temperature") return `${p.heater ?? ""} → ${p.target ?? ""} °C`;
  if (e.action === "set_fan") return `${p.fan ?? ""} → ${p.percent ?? ""} %`;
  if (e.action === "move_axis") return `${p.axis ?? ""} ${p.distance > 0 ? "+" : ""}${p.distance ?? ""} mm`;
  if (e.action === "home") return p.axes || "";
  if (e.action === "pid_calibrate") return `${p.target ?? ""} °C`;
  if (e.action === "heaters_off" && p.idle_min) return `after ${p.idle_min} min idle`;
  return "";
}
async function ctlLoadLog() {
  const host = document.getElementById("ctl-log-list");
  if (!host || !_ctlPrinterId) return;
  try {
    const r = await fetch(`/api/control/log?printer=${encodeURIComponent(_ctlPrinterId)}`);
    if (!r.ok) { host.textContent = r.status === 403 ? "You don't have access to the log." : "Could not load the log."; return; }
    const entries = await r.json();
    if (!entries.length) { host.textContent = "Nothing yet."; return; }
    host.innerHTML = entries.map(e => {
      const ok = e.result === "ok";
      const when = new Date(e.time * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
      return `<div class="ctl-log-row">
        <span class="ctl-log-time">${escHtml(when)}</span>
        <span class="ctl-log-user">${escHtml(e.user || "")}</span>
        <span class="ctl-log-what">${escHtml(_CTL_ACTION_LABEL[e.action] || e.action)} <small>${escHtml(_ctlParamText(e))}</small></span>
        <span class="ctl-log-result ${ok ? "ok" : "fail"}">${ok ? "OK" : escHtml(e.result)}</span>
      </div>`;
    }).join("");
  } catch (_) { host.textContent = "Could not load the log."; }
}

function _controlHtml(p) {
  const caps = p.capabilities || [], lim = p.limits || {}, t = _ctlStatus(p);
  const has = c => caps.includes(c);
  const heaters = [["nozzle", "Nozzle", "temp_nozzle"], ["bed", "Bed", "temp_bed"], ["chamber", "Chamber", "temp_chamber"]]
    .filter(([, , c]) => has(c));
  const fans = [["part", "Part cooling", "fan_part"], ["aux", "Auxiliary", "fan_aux"], ["chamber", "Chamber", "fan_chamber"]]
    .filter(([, , c]) => has(c));
  const fanSpeeds = (p.status || {}).FanSpeed || {};
  const calib = has("level") || has("pid") || has("shaper");
  const motionOk = has("move") || has("home");

  const tile = ([h, label]) => `<div class="ctl-tile${t[h].tgt ? " on" : ""}" id="ctl-tile-${h}">
      <span class="ctl-tile-name">${label}</span>
      <span class="ctl-tile-cur"><span id="ctl-cur-${h}">${Math.round(t[h].cur)}</span>°</span>
      <span class="ctl-tile-tgt">target <span id="ctl-tgt-${h}">${t[h].tgt ? Math.round(t[h].tgt) + "°" : "off"}</span></span>
    </div>`;

  const heaterRow = ([h, label]) => {
    const max = (lim[h] || {}).max ?? 300;
    return `<div class="ctl-row">
      <span class="ctl-row-name">${label}</span>
      <div class="ctl-input-unit"><input type="number" id="ctl-in-${h}" min="0" max="${max}" step="5" value="${t[h].tgt || ""}"
        placeholder="off" title="0–${max} °C" aria-label="${label} target, 0–${max} °C" onkeydown="if (event.key === 'Enter') ctlSetTemp('${h}')"><span>°C</span></div>
      <button class="btn btn-sm btn-primary" onclick="ctlSetTemp('${h}')">Set</button>
      <button class="btn btn-sm btn-secondary" onclick="ctlHeaterOff('${h}')">Off</button>
    </div>`;
  };

  // Slider + a type-in field + Off. The slider and field send on release/Enter
  // (dragging or typing alone does not send); Off always sends 0.
  const fanRow = ([f, label]) => {
    const cur = Math.round(Number(fanSpeeds[f] ?? 0)) || 0;
    return `<div class="ctl-fan">
      <div class="ctl-fan-top"><span class="ctl-row-name">${label}</span><output id="ctl-fan-${f}">${cur}%</output></div>
      <div class="ctl-fan-set" role="group" aria-label="${label} speed">
        <input type="range" class="ctl-fan-slider" id="ctl-fan-range-${f}" min="0" max="100" step="1" value="${cur}"
          oninput="ctlFanSlide('${f}',this.value)" onchange="ctlSetFan('${f}',this.value)" aria-label="${label} speed">
        <div class="ctl-input-unit ctl-fan-num"><input type="number" id="ctl-fan-in-${f}" min="0" max="100" step="1" value="${cur}"
          aria-label="${label} percent" onchange="ctlSetFan('${f}',this.value)"
          onkeydown="if(event.key==='Enter'){ctlSetFan('${f}',this.value);this.blur();}"><span>%</span></div>
        <button class="btn btn-sm btn-secondary" onclick="ctlFanOff('${f}')">Off</button>
      </div>
    </div>`;
  };

  const jog = (a, s, txt) => `<button class="ctl-jog" onclick="ctlMove('${a}',${s})" aria-label="${a}${s > 0 ? "+" : "−"}">${txt}</button>`;
  const homeBtn = (axes, label) => has("home_axes")
    ? `<button class="ctl-jog ctl-home" onclick="ctlHome('${axes}')" aria-label="${label}" title="${label}">⌂</button>` : "<span></span>";

  const calibItem = (title, desc, btn, extra = "") => `<div class="ctl-cal">
      <div class="ctl-cal-text"><strong>${title}</strong><span>${desc}</span></div>${btn}${extra}</div>`;

  const tabs = [["heat", "Heat", heaters.length], ["fans", "Fans", fans.length], ["move", "Move", motionOk],
                ["calib", "Calibrate", calib], ["log", "Log", true]].filter(x => x[2]);
  if (!tabs.some(x => x[0] === _ctlTab)) _ctlTab = tabs[0][0];
  const pane = (name, body) => `<div class="ctl-pane" data-pane="${name}" role="tabpanel" ${name === _ctlTab ? "" : "hidden"}>${body}</div>`;

  return `<div class="modal-box ctl-box" role="dialog" aria-label="Advanced control">
    <div class="ctl-head">
      <div class="ctl-title">${_ICON_WRENCH}<div><h3>${escHtml(p.name || "Printer")}</h3>
        <span class="ctl-sub">Changes happen immediately</span></div></div>
      <span class="ctl-state" id="ctl-state" data-state="${escAttr(p.state || "")}">${_ctlStateLabel(p.state)}</span>
      <button class="ctl-close" onclick="closeControl()" aria-label="Close">✕</button>
    </div>
    ${p.controls_verified ? "" : `<div class="ctl-preview">Not yet tested on this printer model. Commands are sent to the printer, so stay with it and check that each one does what it should.</div>`}
    ${heaters.length ? `<div class="ctl-tiles">${heaters.map(tile).join("")}</div>` : ""}
    <div class="ctl-idle-text" id="ctl-idle-text" hidden></div>

    <div class="ctl-tabs" role="tablist">
      ${tabs.map(([k, label]) => `<button class="ctl-tab${k === _ctlTab ? " active" : ""}" role="tab" aria-selected="${k === _ctlTab}"
          data-tab="${k}" onclick="ctlTab('${k}')">${label}</button>`).join("")}
    </div>

    ${heaters.length ? pane("heat", `
      <div class="ctl-presets"><span class="ctl-label">Presets</span>
        ${CTL_PRESETS.map((pr, i) => `<button class="ctl-chip" onclick="ctlPreset(${i})">${pr.name} <small>${pr.nozzle}/${pr.bed}</small></button>`).join("")}</div>
      <div class="ctl-list">${heaters.map(heaterRow).join("")}</div>
      <button class="btn btn-danger ctl-alloff" onclick="ctlAllOff()">Turn off all heating</button>
      <div class="ctl-guard">
        <div class="ctl-cal-text"><strong>Idle guard</strong><span>Turns heating off when the printer has been idle this long (0 = off).</span></div>
        <div class="ctl-guard-set" id="ctl-idle"></div>
      </div>`) : ""}

    ${fans.length ? pane("fans", `<div class="ctl-list">${fans.map(fanRow).join("")}</div>`) : ""}

    ${motionOk ? pane("move", `
      <p id="ctl-motion-note" class="ctl-note hidden">Axes are locked while the printer is printing or paused.</p>
      <div id="ctl-motion" class="ctl-motion">
        <div class="ctl-pads">
          ${has("move") ? `<div class="ctl-pad">
            <span></span>${jog("Y", 1, "Y+")}<span></span>
            ${jog("X", -1, "X−")}${homeBtn("XY", "Home XY")}${jog("X", 1, "X+")}
            <span></span>${jog("Y", -1, "Y−")}<span></span>
          </div>
          <div class="ctl-zcol">${jog("Z", 1, "Z+")}${homeBtn("Z", "Home Z")}${jog("Z", -1, "Z−")}</div>` : ""}
        </div>
        <div class="ctl-motion-foot">
          ${has("move") ? `<div class="ctl-steps" role="group" aria-label="Step size">
            ${[0.1, 1, 10].map(v => `<button class="ctl-step${v === _ctlStep ? " active" : ""}" data-step="${v}" onclick="ctlStep(${v})">${v} mm</button>`).join("")}
          </div>` : ""}
          ${has("home") ? `<button class="btn btn-sm btn-secondary" onclick="ctlHome('XYZ')">⌂ Home all</button>` : ""}
        </div>
      </div>`) : ""}

    ${calib ? pane("calib", `<div class="ctl-list">
      ${has("level") ? calibItem("Auto leveling", "Heats up and probes the bed.",
          `<button class="btn btn-sm btn-secondary" onclick="ctlLevel()">Start</button>`) : ""}
      ${has("pid") ? calibItem("PID calibrate nozzle", "Tunes the nozzle heater for one temperature.",
          `<button class="btn btn-sm btn-secondary" onclick="ctlPidOpen()">Choose…</button>`,
          `<div id="ctl-pid-box" class="ctl-pid-box" hidden>
            <p>Use the temperature you print at most (PLA 210–220 °C, PETG 240–255 °C). Takes a few minutes; stay with the printer.</p>
            <div class="ctl-pid">
              <div class="ctl-input-unit"><input type="number" id="ctl-pid-target" min="150" max="280" step="5" value="220"
                aria-label="PID target temperature" onkeydown="if (event.key === 'Enter') ctlPid()"><span>°C</span></div>
              <button class="btn btn-sm btn-primary" onclick="ctlPid()">Start</button>
              <button class="btn btn-sm btn-secondary" onclick="ctlPidOpen(false)">Cancel</button>
            </div>
          </div>`) : ""}
      ${has("shaper") ? calibItem("Resonance test", "Input shaper: shakes the toolhead and bed. Keep the bed clear.",
          `<button class="btn btn-sm btn-secondary" onclick="ctlShaper()">Start</button>`) : ""}
    </div>`) : ""}

    ${pane("log", `<div id="ctl-log-list" class="ctl-log-list">Loading…</div>`)}
  </div>`;
}
