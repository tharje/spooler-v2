/* Spooler frontend: File browser, upload, print options, toast. Plain scripts sharing one global scope; load order is in index.html. */
"use strict";

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

