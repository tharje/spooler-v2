// Service logbook: what was done to each printer, and reminders by print hours.
"use strict";
const servicePanel = document.getElementById("panel-service");
const btnService   = document.getElementById("btn-service");
let _service = null;

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
}

function _printerName(id) {
  return (_service.printers.find(p => p.id === id) || {}).name || id;
}

function renderService() {
  const host = document.getElementById("service-body");
  const d = _service;
  if (!d) { host.textContent = "Loading…"; return; }
  if (!d.printers.length) { host.innerHTML = '<p class="service-empty">Add a printer first.</p>'; return; }
  const popts = d.printers.map(p => `<option value="${escAttr(p.id)}">${escHtml(p.name)}</option>`).join("");
  const kopts = d.kinds.map(k => `<option value="${escAttr(k)}">${escHtml(_kindLabel(k))}</option>`).join("");
  const tasks = d.tasks.map(t => `
    <div class="service-row ${t.due ? "due" : ""}">
      <div class="service-main">
        <div class="service-title">${escHtml(t.name)} <span class="service-sub">· ${escHtml(_printerName(t.printer_id))}</span></div>
        <div class="service-meta">${t.due
          ? `<strong>Due</strong> — ${t.hours_since} h since last time (every ${t.interval_hours} h)`
          : `${t.remaining} h left (every ${t.interval_hours} h)`}${t.last_done ? ` · last done ${escHtml(t.last_done)}` : ""}</div>
      </div>
      <div class="service-actions">
        <button class="btn btn-primary btn-sm" data-done="${escAttr(t.id)}">Done</button>
        <button class="btn btn-secondary btn-sm" data-del-task="${escAttr(t.id)}" title="Remove this reminder">Remove</button>
      </div>
    </div>`).join("") || '<p class="service-empty">No reminders yet.</p>';
  const log = d.log.map(e => `
    <div class="service-row">
      <div class="service-main">
        <div class="service-title">${escHtml(_kindLabel(e.kind))} <span class="service-sub">· ${escHtml(_printerName(e.printer_id))}</span></div>
        <div class="service-meta">${escHtml(e.date)} · at ${e.hours} print hours${e.note ? ` — ${escHtml(e.note)}` : ""}</div>
      </div>
      <div class="service-actions"><button class="btn btn-secondary btn-sm" data-del-log="${escAttr(e.id)}">Delete</button></div>
    </div>`).join("") || '<p class="service-empty">Nothing logged yet.</p>';
  host.innerHTML = `
    <div class="service-hours">${d.printers.map(p => `<span><strong>${p.hours} h</strong> ${escHtml(p.name)}</span>`).join("")}</div>
    <h4 class="service-h">Reminders</h4>${tasks}
    <details class="service-form"><summary>Add reminder</summary>
      <select id="svc-t-printer">${popts}</select>
      <input id="svc-t-name" type="text" maxlength="80" placeholder="What (e.g. Lubricate Z rods)" />
      <select id="svc-t-kind">${kopts}</select>
      <input id="svc-t-hours" type="number" min="1" max="100000" placeholder="Every … print hours" />
      <button id="svc-t-add" class="btn btn-primary btn-sm">Add reminder</button>
    </details>
    <h4 class="service-h">Logbook</h4>
    <details class="service-form"><summary>Log service</summary>
      <select id="svc-l-printer">${popts}</select>
      <select id="svc-l-kind">${kopts}</select>
      <input id="svc-l-date" type="date" value="${new Date().toISOString().slice(0, 10)}" />
      <input id="svc-l-note" type="text" maxlength="500" placeholder="Note (optional)" />
      <button id="svc-l-add" class="btn btn-primary btn-sm">Save entry</button>
    </details>${log}`;
}

async function _serviceAct(fn) {
  try { await fn(); await loadService(); }
  catch (e) { toast(e.message, true); }
}

document.getElementById("service-body").addEventListener("click", (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  const v = (id) => document.getElementById(id).value;
  if (t.id === "svc-t-add") {
    _serviceAct(() => _serviceCall("POST", "/api/service/tasks", {
      printer_id: v("svc-t-printer"), name: v("svc-t-name"), kind: v("svc-t-kind"), interval_hours: v("svc-t-hours") }));
  } else if (t.id === "svc-l-add") {
    _serviceAct(() => _serviceCall("POST", "/api/service/log", {
      printer_id: v("svc-l-printer"), kind: v("svc-l-kind"), date: v("svc-l-date"), note: v("svc-l-note") }));
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
