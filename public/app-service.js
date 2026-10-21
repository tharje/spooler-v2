// Service logbook: what was done to each printer, and reminders by print hours.
"use strict";
const servicePanel = document.getElementById("panel-service");
const btnService   = document.getElementById("btn-service");
let _service = null;
let _serviceDuePrinters = new Set();      // printers with a reminder that is due (card notice)

function _kindLabel(k) { return (k || "other").replace("_", " ").replace(/^./, c => c.toUpperCase()); }

async function _serviceCall(method, path, body) {
  const r = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || "Something went wrong");
  return d;
}

async function loadService() {
  try {
    const r = await fetch("/api/service");
    if (!r.ok) return;
    _service = await r.json();
    _setServiceBadge();
    if (servicePanel.classList.contains("open")) renderService();
  } catch (_) { /* server may not be ready yet */ }
}

function _setServiceBadge() {
  const due = (_service?.tasks || []).filter(t => t.due).length;
  const b = document.getElementById("service-badge");
  b.hidden = !due;
  b.textContent = due || "";
  _serviceDuePrinters = new Set((_service?.tasks || []).filter(t => t.due).map(t => t.printer_id));
  Object.values(printers).forEach(renderPrinter);
}

function _printerName(id) {
  return (_service.printers.find(p => p.id === id) || {}).name || id;
}

const _KIND_ICON = {
  lubrication:   '<path d="M12 2.7s6 6.2 6 10.6a6 6 0 0 1-12 0C6 8.900 12 2.700 12 2.700z"/>',
  nozzle:        '<path d="M8 3h8v5l-2 3v3l-2 3-2-3v-3L8 8z"/>',
  belt:          '<rect x="3" y="8" width="18" height="8" rx="4"/><circle cx="8" cy="12" r="1.500"/><circle cx="16" cy="12" r="1.500"/>',
  cleaning:      '<path d="M12 3l1.800 5.200L19 10l-5.200 1.800L12 17l-1.800-5.200L5 10l5.200-1.800z"/>',
  calibration:   '<path d="M4 20L20 4M7 17l-2-2M10 14l-2-2M13 11l-2-2M16 8l-2-2"/>',
  part_replaced: '<path d="M14.700 6.300a1 1 0 0 0 0 1.400l1.600 1.600a1 1 0 0 0 1.400 0l3.800-3.800a6 6 0 0 1-7.900 7.900l-6.900 6.900a2.100 2.100 0 0 1-3-3l6.900-6.900a6 6 0 0 1 7.900-7.900z"/>',
  other:         '<circle cx="12" cy="12" r="1"/><circle cx="5" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
};
const _SERVICE_PRESETS = [
  { name: "Lubricate Z rods",  kind: "lubrication", hours: 200 },
  { name: "Clean the nozzle",  kind: "nozzle",      hours: 100 },
  { name: "Check the belts",   kind: "belt",        hours: 300 },
  { name: "Clean the plate",   kind: "cleaning",    hours: 50 },
  { name: "Clean or replace the filter", kind: "cleaning", hours: 300 },
];

function _kindIcon(k) {
  return `<span class="service-icon k-${escAttr(k || "other")}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${_KIND_ICON[k] || _KIND_ICON.other}</svg></span>`;
}

// A part counts down like a consumable: the bar is what is left until it is due.
function _left(t) { return Math.max(0, Math.min(100, Math.round(100 * (1 - t.since / t.interval)))); }
function _tone(t) { const l = _left(t); return t.due || l <= 10 ? "red" : l <= 30 ? "yellow" : "green"; }
function _unitOf(t) { return t.unit === "days" ? "days" : "h"; }

function _taskHtml(t) {
  const left = _left(t);
  return `
    <div class="service-task ${t.due ? "due" : ""}">
      ${_kindIcon(t.kind)}
      <div class="service-main">
        <div class="service-title-row">
          <span class="service-title">${escHtml(t.name)}</span>
          <span class="service-left ${_tone(t)}">${t.due ? "Due" : `${t.remaining} ${_unitOf(t)} left`}</span>
        </div>
        <div class="service-bar" role="progressbar" aria-valuenow="${left}" aria-valuemin="0" aria-valuemax="100" aria-label="${escAttr(t.name)}">
          <div class="service-bar-fill ${_tone(t)}" style="width:${left}%"></div>
        </div>
        <div class="service-meta">${left}% left · every ${t.interval} ${_unitOf(t)}${t.last_done ? ` · last ${escHtml(t.last_done)}` : ""}</div>
      </div>
      <div class="service-actions">
        <button class="btn ${t.due || left <= 30 ? "btn-primary" : "btn-secondary"} btn-sm" data-done="${escAttr(t.id)}" title="Serviced: start the countdown again">Reset</button>
        <button class="service-x" data-del-task="${escAttr(t.id)}" title="Remove this part" aria-label="Remove this part">×</button>
      </div>
    </div>`;
}

const _ERR_CATEGORY_LABEL = {
  thermal: "Thermal", motion: "Motion", sensor: "Sensor", fan: "Fan", leveling: "Leveling",
  hardware: "Hardware", connection: "Connection", system: "System",
  filament_runout: "Filament runout", filament_feed: "Filament feed",
  filament_tangle: "Filament tangle", unknown: "Unknown",
};

function _errHtml(e) {
  const when = e.timestamp ? escHtml(e.timestamp.replace("T", " ")) : "Unknown time";
  const code = e.error_code != null ? ` (code ${escHtml(String(e.error_code))})` : "";
  return `
    <div class="service-entry">
      <span class="service-icon k-hardware"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg></span>
      <div class="service-main">
        <div class="service-title">${escHtml(e.error_message || "Print error")}${code}</div>
        <div class="service-meta">${when}${e.filename ? ` · ${escHtml(e.filename)}` : ""}</div>
      </div>
    </div>`;
}

// Where to ask when a code has nothing (or little) written about it yet.
const _ISSUE_REPO_URL = "https://github.com/tharje/spooler-v2/issues/new";

function _codeIssueUrl(c, printerType) {
  const title = `Error code ${c.code} (${printerType || "?"}): more detail wanted`;
  const body = `I'd like a more thorough description / fix for this error code.\n\n` +
    `Printer type: ${printerType || "unknown"}\nCode: ${c.code}\n` +
    `Current short text: ${c.message || "(none)"}${c.action ? `\nCurrent tip: ${c.action}` : ""}\n\n` +
    `What happened when you saw it, and what fixed it (if anything):\n`;
  return `${_ISSUE_REPO_URL}?title=${encodeURIComponent(title)}&body=${encodeURIComponent(body)}`;
}

function _codeDetailHtml(c, printerType) {
  const hasTip = !!c.action;
  return `
    <div class="service-code-detail">
      <div class="service-code-row"><strong>Category</strong><span>${escHtml(_ERR_CATEGORY_LABEL[c.category] || c.category || "Unknown")}</span></div>
      <div class="service-code-row"><strong>What it means</strong><span>${escHtml(c.message || "Not described yet.")}</span></div>
      ${hasTip ? `<div class="service-code-row"><strong>Fix tip</strong><span>${escHtml(c.action)}</span></div>` : ""}
      <div class="service-code-ask">
        ${hasTip ? "Know more about this one, or found a different fix?" : "No fix tip for this one yet — know what causes or fixes it?"}
        <a href="${_codeIssueUrl(c, printerType)}" target="_blank" rel="noopener">Tell us on GitHub</a>
      </div>
    </div>`;
}

function _errorReferenceHtml(codes, printerType) {
  if (!codes || !codes.length) return '<p class="service-empty">No error code reference for this printer type yet.</p>';
  return codes.map(c => `
    <details class="service-code">
      <summary>
        <span class="service-code-num">${escHtml(String(c.code))}</span>
        <span class="service-code-msg">${escHtml(c.message || "Unknown code")}</span>
        <span class="service-chev" aria-hidden="true">›</span>
      </summary>
      ${_codeDetailHtml(c, printerType)}
    </details>`).join("");
}

function _errorPanelHtml(d, p) {
  const errors = (d.errors || []).filter(e => e.printer_id === p.id);
  const codes = (d.error_code_reference || {})[p.type];
  return `
    <div class="service-machine-head">
      <button type="button" class="service-back" data-back-errors="1" aria-label="Back">‹</button>
      <div><div class="service-printer">${escHtml(p.name)}</div><div class="service-meta">Error codes</div></div>
    </div>
    <div class="service-h">Error history</div>
    <div class="service-card">${errors.length ? errors.map(_errHtml).join("") : '<p class="service-empty">No print errors recorded.</p>'}</div>
    <div class="service-h">Error code reference</div>
    <div class="service-card">${_errorReferenceHtml(codes, p.type)}</div>`;
}

function _logHtml(e) {
  return `
    <div class="service-entry">
      ${_kindIcon(e.kind)}
      <div class="service-main">
        <div class="service-title">${escHtml(_kindLabel(e.kind))}${e.note ? ` <span class="service-note">— ${escHtml(e.note)}</span>` : ""}</div>
        <div class="service-meta">${escHtml(e.date)} · at ${e.hours} print hours</div>
      </div>
      <button class="service-x" data-del-log="${escAttr(e.id)}" title="Delete this entry" aria-label="Delete this entry">×</button>
    </div>`;
}

let _serviceView = null;          // id of the printer whose parts are open, or null for the overview
let _serviceErrorView = false;    // within _serviceView: showing the error codes panel instead of parts/logbook

function _overviewHtml(d) {
  const dueTotal = d.tasks.filter(t => t.due).length;
  const cards = d.printers.map(p => {
    const tasks = d.tasks.filter(t => t.printer_id === p.id).sort((a, b) => _left(a) - _left(b));
    const due = tasks.filter(t => t.due).length;
    const rows = tasks.slice(0, 4).map(t => `
      <div class="service-mini">
        <span class="service-mini-name">${escHtml(t.name)}</span>
        <div class="service-bar"><div class="service-bar-fill ${_tone(t)}" style="width:${_left(t)}%"></div></div>
        <span class="service-mini-pct ${_tone(t)}">${t.due ? "Due" : _left(t) + "%"}</span>
      </div>`).join("");
    return `
      <button type="button" class="service-card service-machine" data-open-printer="${escAttr(p.id)}">
        <div class="service-card-head">
          <span class="service-printer">${escHtml(p.name)}</span>
          ${due ? `<span class="service-due-pill">${due} due</span>` : ""}
          <span class="service-hours-pill">${p.hours} print hours</span>
          <span class="service-chev" aria-hidden="true">›</span>
        </div>
        ${rows || '<div class="service-meta">No parts tracked yet. Tap to add some.</div>'}
        ${tasks.length > 4 ? `<div class="service-meta">+ ${tasks.length - 4} more</div>` : ""}
      </button>`;
  }).join("");
  return `${dueTotal ? `<div class="service-alert">${dueTotal} service ${dueTotal === 1 ? "reminder is" : "reminders are"} due</div>` : ""}${cards}`;
}

function _machineHtml(d, p) {
  const open = new Set([...document.querySelectorAll("#service-body details[open]")].map(x => x.dataset.form));
  const keep = (id) => document.getElementById(id)?.value;
  const draft = { name: keep("svc-t-name"), hours: keep("svc-t-hours"), note: keep("svc-l-note") };
  const kopts = d.kinds.map(k => `<option value="${escAttr(k)}">${escHtml(_kindLabel(k))}</option>`).join("");
  const tasks = d.tasks.filter(t => t.printer_id === p.id).sort((a, b) => _left(a) - _left(b));
  const log = d.log.filter(e => e.printer_id === p.id);
  return `
    <div class="service-machine-head">
      <button type="button" class="service-back" data-back="1" aria-label="Back to all printers">‹</button>
      <div><div class="service-printer">${escHtml(p.name)}</div><div class="service-meta service-total">Total: <strong>${p.hours} print hours</strong>
        <button type="button" class="service-link" data-edit-hours="1">Change</button></div>
        <div class="service-hours-edit" hidden>
          <input id="svc-hours" type="number" min="0" step="0.1" value="${escAttr(p.hours)}" aria-label="Total print hours" />
          <button class="btn btn-primary btn-sm" id="svc-hours-save">Save</button>
          <button class="btn btn-secondary btn-sm" data-edit-hours="0">Cancel</button>
        </div></div>
    </div>
    <div class="service-h">Parts</div>
    <div class="service-card">${tasks.length ? tasks.map(_taskHtml).join("") : '<p class="service-empty">No parts tracked yet. Add one below.</p>'}</div>
    <div class="service-toolbar">
      <button class="btn btn-primary btn-sm" data-toggle="task">Add part</button>
      <button class="btn btn-secondary btn-sm" data-toggle="log">Log service</button>
      <button class="btn btn-secondary btn-sm" data-open-errors="1">Error codes</button>
    </div>
    <details class="service-form" data-form="task" ${open.has("task") ? "open" : ""}><summary hidden></summary>
      <div class="service-form-title">Add part</div>
      <div class="service-chips">${_SERVICE_PRESETS.map((x, i) =>
        `<button type="button" class="service-chip" data-preset="${i}">${escHtml(x.name)} · ${x.hours} h</button>`).join("")}</div>
      <input id="svc-t-name" type="text" maxlength="80" placeholder="What (e.g. Lubricate Z rods)" value="${escAttr(draft.name || "")}" />
      <select id="svc-t-kind">${kopts}</select>
      <div class="service-every">
        <input id="svc-t-hours" type="number" min="1" max="100000" placeholder="Every …" value="${escAttr(draft.hours || "")}" />
        <select id="svc-t-unit"><option value="hours">print hours</option><option value="days">days</option></select>
      </div>
      <button id="svc-t-add" class="btn btn-primary btn-sm">Add part</button>
    </details>
    <details class="service-form" data-form="log" ${open.has("log") ? "open" : ""}><summary hidden></summary>
      <div class="service-form-title">Log service</div>
      <select id="svc-l-kind">${kopts}</select>
      <input id="svc-l-date" type="date" value="${new Date().toISOString().slice(0, 10)}" />
      <input id="svc-l-note" type="text" maxlength="500" placeholder="Note (optional)" value="${escAttr(draft.note || "")}" />
      <button id="svc-l-add" class="btn btn-primary btn-sm">Save entry</button>
    </details>
    <div class="service-h">Logbook</div>
    <div class="service-card">${log.length ? log.map(_logHtml).join("") : '<p class="service-empty">Nothing logged yet.</p>'}</div>`;
}

function renderService() {
  const host = document.getElementById("service-body");
  const d = _service;
  if (!d) { host.textContent = "Loading…"; return; }
  if (!d.printers.length) { host.innerHTML = '<p class="service-empty">Add a printer first.</p>'; return; }
  const p = d.printers.find(x => x.id === _serviceView);
  if (!p) { _serviceView = null; _serviceErrorView = false; }
  host.innerHTML = !p ? _overviewHtml(d) : _serviceErrorView ? _errorPanelHtml(d, p) : _machineHtml(d, p);
}

async function _serviceAct(fn) {
  try { await fn(); await loadService(); }
  catch (e) { toast(e.message, true); }
}

document.getElementById("service-body").addEventListener("click", (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  const v = (id) => document.getElementById(id).value;
  if (t.dataset.openPrinter) {
    _serviceView = t.dataset.openPrinter;
    renderService();
  } else if (t.dataset.openErrors) {
    _serviceErrorView = true;
    renderService();
  } else if (t.dataset.backErrors) {
    _serviceErrorView = false;
    renderService();
  } else if (t.dataset.editHours) {
    const box = document.querySelector(".service-hours-edit"), tot = document.querySelector(".service-total");
    box.hidden = t.dataset.editHours !== "1"; tot.hidden = !box.hidden;
    if (!box.hidden) document.getElementById("svc-hours").select();
  } else if (t.id === "svc-hours-save") {
    _serviceAct(() => _serviceCall("POST", "/api/service/hours", { printer_id: _serviceView, hours: v("svc-hours") }));
  } else if (t.dataset.back) {
    _serviceView = null;
    renderService();
  } else if (t.dataset.toggle) {
    document.querySelectorAll("#service-body details.service-form").forEach(f => {
      f.open = f.dataset.form === t.dataset.toggle ? !f.open : false;
    });
  } else if (t.dataset.preset) {
    const x = _SERVICE_PRESETS[+t.dataset.preset];
    document.getElementById("svc-t-name").value = x.name;
    document.getElementById("svc-t-kind").value = x.kind;
    document.getElementById("svc-t-hours").value = x.hours;
  } else if (t.id === "svc-t-add") {
    _serviceAct(() => _serviceCall("POST", "/api/service/tasks", {
      printer_id: _serviceView, name: v("svc-t-name"), kind: v("svc-t-kind"), interval: v("svc-t-hours"), unit: v("svc-t-unit") }));
  } else if (t.id === "svc-l-add") {
    _serviceAct(() => _serviceCall("POST", "/api/service/log", {
      printer_id: _serviceView, kind: v("svc-l-kind"), date: v("svc-l-date"), note: v("svc-l-note") }));
  } else if (t.dataset.done) {
    const task = _service.tasks.find(x => x.id === t.dataset.done);
    _serviceAct(() => _serviceCall("POST", "/api/service/log", {
      printer_id: task.printer_id, kind: task.kind || "other", note: task.name, task_id: task.id }));
  } else if (t.dataset.delTask) {
    _serviceAct(() => _serviceCall("DELETE", "/api/service/tasks/" + encodeURIComponent(t.dataset.delTask)));
  } else if (t.dataset.delLog) {
    if (confirm("Delete this logbook entry?"))
      _serviceAct(() => _serviceCall("DELETE", "/api/service/log/" + encodeURIComponent(t.dataset.delLog)));
  }
});

const openService = () => {
  servicePanel.classList.add("open");
  historyBackdrop.classList.add("open");
  btnService.classList.add("active");
  renderService();
  loadService();
};
const closeService = () => {
  servicePanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  btnService.classList.remove("active");
};
btnService.addEventListener("click", openService);
document.getElementById("btn-service-close").addEventListener("click", closeService);
