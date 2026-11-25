// T20: print queue. A flat list of jobs with requirements; a free, matching
// printer gets a job "assigned" to it, and the queue panel is where you
// confirm the plate is clear and send it -- nothing is ever sent by itself.
"use strict";
const queuePanel = document.getElementById("panel-queue");
const btnQueue    = document.getElementById("btn-queue");
let _queueJobs = [];
let _queueUploading = false;

async function _queueCall(method, path, body) {
  const r = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || "Something went wrong");
  return d;
}

async function loadQueue() {
  try {
    const d = await _queueCall("GET", "/api/queue");
    _queueJobs = d.jobs || [];
    _setQueueBadge();
    if (queuePanel.classList.contains("open")) renderQueue();
  } catch (_) { /* server may not be ready, or the feature is off */ }
}

function _setQueueBadge() {
  const ready = _queueJobs.filter(j => j.status === "assigned").length;
  const b = document.getElementById("queue-badge");
  b.hidden = !ready;
  b.textContent = ready || "";
}

const _STATUS_LABEL = {
  queued: "In queue", assigned: "Ready", printing: "Printing", done: "Done", error: "Error",
  stopped: "Stopped", cancelled: "Cancelled",
};
const _FINISHED = ["done", "error", "stopped", "cancelled"];

function _printerName(id) { return (printers[id] || {}).name || id; }

function _jobRequirementsText(j) {
  const bits = [];
  if (j.printer_id) bits.push(_printerName(j.printer_id));
  else if (j.printer_type) bits.push(j.printer_type.toUpperCase());
  if (j.material) bits.push(j.material);
  if (j.colour) bits.push(j.colour);
  if (j.quantity > 1) bits.push(`×${j.quantity}`);
  return bits.join(" · ") || "Any printer";
}

// Who put the job in the queue, and when ("Added by anna · 16.11.2026, 20:23").
// The owner is empty when login is off.
function _jobAddedText(j) {
  const d = new Date(j.added_at);
  const when = isNaN(d) ? "" : d.toLocaleString([], { dateStyle: "short", timeStyle: "short" });
  if (j.owner) return when ? `Added by ${j.owner} · ${when}` : `Added by ${j.owner}`;
  return when ? `Added ${when}` : "";
}

// All text interpolated below is escaped with escHtml/escAttr (same helpers
// used by app-service.js and app-stats.js for the same kind of user-entered
// strings: filenames, free-text requirement fields).
// place/count: where a job is in the list of those still in the queue (0-based)
// and how many there are, for the move buttons; left out for finished jobs.
function _jobHtml(j, place, count) {
  const canMove = count > 1 && (window.SPOOLER_PERMS || []).includes("queue.manage");
  const canCancel = j.status === "queued" || j.status === "assigned";
  const canRemove = _FINISHED.includes(j.status);
  return `<div class="queue-job" data-job="${escAttr(j.id)}">
    <div class="queue-job-main">
      <div class="queue-job-name">${escHtml(j.filename)}</div>
      <div class="queue-job-reqs">${escHtml(j.status === "printing"
        ? `On ${_printerName(j.assigned_printer_id)}` : _jobRequirementsText(j))}</div>
      ${_jobAddedText(j) ? `<div class="queue-job-added">${escHtml(_jobAddedText(j))}</div>` : ""}
      ${j.error ? `<div class="queue-job-error">${escHtml(j.error)}</div>` : ""}
    </div>
    <div class="queue-job-status queue-status-${j.status}">${_STATUS_LABEL[j.status] || j.status}</div>
    <div class="queue-job-actions">
      ${canMove ? `<button class="btn btn-secondary btn-sm" data-move="up" data-job-id="${escAttr(j.id)}"
          title="Move up" aria-label="Move up"${place === 0 ? " disabled" : ""}>▲</button>
        <button class="btn btn-secondary btn-sm" data-move="down" data-job-id="${escAttr(j.id)}"
          title="Move down" aria-label="Move down"${place === count - 1 ? " disabled" : ""}>▼</button>` : ""}
      ${j.status === "assigned"
        ? `<button class="btn btn-primary btn-sm" data-start="${escAttr(j.id)}">Plate clear – start</button>`
        : ""}
      ${canCancel ? `<button class="btn btn-secondary btn-sm" data-cancel="${escAttr(j.id)}">Cancel</button>` : ""}
      ${canRemove ? `<button class="btn btn-secondary btn-sm" data-remove="${escAttr(j.id)}">Remove</button>` : ""}
    </div>
  </div>`;
}

function _printerOptionsHtml() {
  return Object.values(printers).map(p => `<option value="${escAttr(p.id)}">${escHtml(p.name)}</option>`).join("");
}

function renderQueue() {
  const host = document.getElementById("queue-body");
  // the server sends them next-first: what's printing, jobs ready on a printer, then the waiting ones in order
  const printing = _queueJobs.filter(j => j.status === "printing");
  const active = _queueJobs.filter(j => j.status === "queued" || j.status === "assigned");
  const finished = _queueJobs.filter(j => _FINISHED.includes(j.status));
  host.innerHTML = `
    <details class="queue-form" open>
      <summary>Add to queue</summary>
      <div class="queue-dropzone" id="queue-dropzone">Drop a .gcode file here, or
        <button class="btn btn-secondary btn-sm" id="btn-queue-pick">choose a file</button>
        <input type="file" id="queue-file-input" accept=".gcode,.gco,.bgcode" hidden />
      </div>
      <div class="queue-form-grid">
        <label>Printer type<select id="q-printer-type"><option value="">Any</option>
          <option value="cc1">CC1</option><option value="cc2">CC2</option>
          <option value="moonraker">Klipper/Moonraker</option><option value="prusa">Prusa</option></select></label>
        <label>Specific printer<select id="q-printer-id"><option value="">Any matching</option>${_printerOptionsHtml()}</select></label>
        <label>Material<input id="q-material" type="text" maxlength="40" placeholder="e.g. PLA" /></label>
        <label>Colour<input id="q-colour" type="text" maxlength="40" placeholder="e.g. Black" /></label>
        <label>Quantity<input id="q-quantity" type="number" min="1" value="1" /></label>
      </div>
      <div id="queue-upload-progress" class="files-upload-progress" style="display:none">
        <div id="queue-upload-bar" class="files-upload-bar"></div>
        <span id="queue-upload-status"></span>
      </div>
    </details>
    ${printing.length ? `<div class="service-h">Printing</div>
      <div class="service-card">${printing.map(j => _jobHtml(j)).join("")}</div>` : ""}
    <div class="service-h">In queue</div>
    <div class="service-card">${active.length ? active.map((j, n) => _jobHtml(j, n, active.length)).join("")
        : '<p class="service-empty">Nothing queued.</p>'}</div>
    ${finished.length ? `<div class="service-h">Finished</div>
      <div class="service-card">${finished.map(j => _jobHtml(j)).join("")}</div>` : ""}`;
}

function _uploadToQueue(file) {
  if (_queueUploading) return;
  const v = (id) => document.getElementById(id).value;
  const params = new URLSearchParams({
    name: file.name, printer_type: v("q-printer-type"), printer_id: v("q-printer-id"),
    material: v("q-material"), colour: v("q-colour"), quantity: v("q-quantity"),
  });
  const prog = document.getElementById("queue-upload-progress");
  const bar = document.getElementById("queue-upload-bar");
  const status = document.getElementById("queue-upload-status");
  _queueUploading = true;
  prog.style.display = "";
  bar.style.width = "0";
  status.textContent = "0%";
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/queue/upload?${params.toString()}`);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    bar.style.width = pct + "%";
    status.textContent = pct < 100 ? `${pct}%` : "Checking file…";
  };
  xhr.onload = () => {
    _queueUploading = false;
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status === 200 && body.ok) {
      status.textContent = "Added to queue";
      toast(`Added to queue: ${file.name}`);
      loadQueue();
    } else {
      status.textContent = body.error || `Could not add to the queue (HTTP ${xhr.status})`;
      status.style.color = "var(--red)";
    }
  };
  xhr.onerror = () => { _queueUploading = false; status.textContent = "Network error"; status.style.color = "var(--red)"; };
  xhr.send(file);
}

document.getElementById("queue-body").addEventListener("click", (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  if (t.id === "btn-queue-pick") {
    document.getElementById("queue-file-input").click();
  } else if (t.dataset.start) {
    // the start dialog: filament slot per tool, leveling, timelapse, plate
    const job = _queueJobs.find(j => j.id === t.dataset.start);
    if (job) openQueueStartOpts(job);
  } else if (t.dataset.move) {
    _queueCall("PATCH", `/api/queue/${encodeURIComponent(t.dataset.jobId)}`, { move: t.dataset.move })
      .then(loadQueue).catch(e => toast(e.message, true));
  } else if (t.dataset.cancel) {
    _queueCall("PATCH", `/api/queue/${encodeURIComponent(t.dataset.cancel)}`, { status: "cancelled" })
      .then(loadQueue).catch(e => toast(e.message, true));
  } else if (t.dataset.remove) {
    _queueCall("DELETE", `/api/queue/${encodeURIComponent(t.dataset.remove)}`)
      .then(loadQueue).catch(e => toast(e.message, true));
  }
});

document.getElementById("queue-body").addEventListener("change", (ev) => {
  if (ev.target.id === "queue-file-input" && ev.target.files[0]) {
    _uploadToQueue(ev.target.files[0]);
    ev.target.value = "";
  }
});

const openQueue = () => {
  queuePanel.classList.add("open");
  renderQueue();
  loadQueue();
};
const closeQueue = () => {
  queuePanel.classList.remove("open");
};
