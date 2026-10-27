/* Spooler frontend: Spoolman, filament ledger, EAN lookup. Plain scripts sharing one global scope; load order is in index.html. */
"use strict";

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

// Spools of the same brand, material and colour are shown as one stack; the
// stack opens to the individual reels. View state lives only in the page.
const _spoolView = { q: "", material: "", low: false, open: new Set() };

function _spoolGroupKey(s) {
  const f = s.filament || {};
  return [f.vendor?.name || "", f.material || "", f.name || "", spoolColorHex(s).toLowerCase()].join("|");
}

function _spoolGroups(list) {
  const map = new Map();
  for (const s of list) {
    const k = _spoolGroupKey(s);
    if (!map.has(k)) map.set(k, { key: k, spools: [] });
    map.get(k).spools.push(s);
  }
  const groups = [...map.values()];
  groups.forEach(g => g.spools.sort((x, y) => spoolRemaining(x) - spoolRemaining(y)));
  // Stacks that need attention come first.
  groups.sort((x, y) => _groupLowRank(x) - _groupLowRank(y) || _groupLabel(x).localeCompare(_groupLabel(y)));
  return groups;
}

function _groupLabel(g) {
  const f = g.spools[0].filament || {};
  return `${f.vendor?.name || "Unknown brand"} ${f.name || ""}`.trim();
}

function _groupTotals(g) {
  const remaining = g.spools.reduce((n, s) => n + spoolRemaining(s), 0);
  const total = g.spools.reduce((n, s) => n + spoolTotal(s), 0);
  return { remaining, total, pct: total > 0 ? Math.round(remaining / total * 100) : 0 };
}

// 0 = every reel empty, 1 = running low, 2 = fine
function _groupLowRank(g) {
  const { remaining, total, pct } = _groupTotals(g);
  if (remaining === 0) return 0;
  return total > 0 && pct < 10 ? 1 : 2;
}

function _spoolMatchesFilter(s) {
  const f = s.filament || {};
  if (_spoolView.material && (f.material || "") !== _spoolView.material) return false;
  if (_spoolView.low && !(spoolRemaining(s) === 0 || spoolPct(s) < 10)) return false;
  const q = _spoolView.q.trim().toLowerCase();
  if (q) {
    const hay = `${f.vendor?.name || ""} ${f.material || ""} ${f.name || ""} ${s.location || ""}`.toLowerCase();
    if (!q.split(/\s+/).every(w => hay.includes(w))) return false;
  }
  return true;
}

function _renderSpoolFilters() {
  const box = document.getElementById("spools-filters");
  if (!box) return;
  const materials = [...new Set(spools.map(s => s.filament?.material).filter(Boolean))].sort();
  const lowCount = spools.filter(s => spoolRemaining(s) === 0 || spoolPct(s) < 10).length;
  box.hidden = spools.length < 2;
  const active = document.activeElement?.id;
  box.innerHTML = `
    <input id="spools-search" type="search" placeholder="Search brand, colour, printer…" value="${escAttr(_spoolView.q)}" />
    <select id="spools-material" aria-label="Material">
      <option value="">All materials</option>
      ${materials.map(m => `<option value="${escAttr(m)}"${m === _spoolView.material ? " selected" : ""}>${escHtml(m)}</option>`).join("")}
    </select>
    <button id="spools-low" class="btn btn-sm ${_spoolView.low ? "btn-primary" : "btn-secondary"}"${lowCount ? "" : " disabled"}>
      Low / empty${lowCount ? ` (${lowCount})` : ""}
    </button>`;
  document.getElementById("spools-search").addEventListener("input", e => {
    _spoolView.q = e.target.value;
    renderSpoolPanel();
  });
  document.getElementById("spools-material").addEventListener("change", e => {
    _spoolView.material = e.target.value;
    renderSpoolPanel();
  });
  document.getElementById("spools-low").addEventListener("click", () => {
    _spoolView.low = !_spoolView.low;
    renderSpoolPanel();
  });
  if (active === "spools-search") {
    const el = document.getElementById("spools-search");
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }
}

function _spoolReelRow(s) {
  const pct = spoolPct(s), remaining = spoolRemaining(s);
  const isEmpty = remaining === 0, isLow = !isEmpty && pct < 10;
  const assignedTo = spoolAssignedTo(s);
  const printerName = assignedTo ? (printerByLocation(assignedTo)?.name || assignedTo) : null;
  return `<div class="spool-reel${isEmpty ? " spool-empty" : isLow ? " spool-low" : ""}">
    <div class="spool-reel-main">
      <span class="spool-reel-id">#${escHtml(s.id)}</span>
      <span class="spool-reel-weight">${remaining}g <small>/ ${spoolTotal(s)}g</small></span>
      ${isEmpty ? '<span class="spool-tag empty">Empty</span>' : isLow ? '<span class="spool-tag low">Low</span>' : ""}
      ${printerName ? `<span class="spool-printer-badge">${escHtml(printerName)}</span>` : '<span class="spool-reel-shelf">In stock</span>'}
    </div>
    <div class="spool-bar-wrap"><div class="spool-bar-fill" style="width:${pct}%${isEmpty ? ";background:var(--red)" : isLow ? ";background:var(--yellow)" : ""}"></div></div>
    <div class="spool-reel-actions">
      <button class="btn btn-sm btn-secondary" onclick="deleteSpool('${escAttr(s.id)}')">Remove</button>
    </div>
  </div>`;
}

function renderSpoolPanel() {
  const list  = document.getElementById("spools-list");
  const empty = document.getElementById("spools-empty");
  _renderSpoolFilters();
  if (!spools.length) {
    list.innerHTML = "";
    empty.style.display = "";
    return;
  }
  empty.style.display = "none";
  const groups = _spoolGroups(spools.filter(_spoolMatchesFilter));
  if (!groups.length) {
    list.innerHTML = '<p class="spools-none">No spools match.</p>';
    return;
  }
  const summary = `<div class="spools-summary">${spools.length} ${spools.length === 1 ? "spool" : "spools"} · ${groups.length} ${groups.length === 1 ? "type" : "types"}</div>`;
  list.innerHTML = summary + groups.map(g => {
    const first = g.spools[0], f = first.filament || {};
    const { remaining, total, pct } = _groupTotals(g);
    const rank = _groupLowRank(g);
    const open = _spoolView.open.has(g.key);
    const n = g.spools.length;
    const locs = [...new Set(g.spools.map(spoolAssignedTo).filter(Boolean).map(l => printerByLocation(l)?.name || l))];
    return `<div class="spool-group${rank === 0 ? " spool-empty" : rank === 1 ? " spool-low" : ""}${open ? " open" : ""}" data-key="${escAttr(g.key)}">
      <button class="spool-group-head" aria-expanded="${open}">
        <span class="spool-group-swatch" style="background:${escAttr(spoolColorHex(first))}"></span>
        <span class="spool-group-info">
          <span class="spool-group-title">${escHtml(f.vendor?.name || "Unknown brand")} <b>${escHtml(f.name || "—")}</b></span>
          <span class="spool-group-sub">
            <span class="spool-material-badge">${escHtml(f.material || "?")}</span>
            <span>${n} ${n === 1 ? "reel" : "reels"}</span>
            ${locs.map(l => `<span class="spool-printer-badge">${escHtml(l)}</span>`).join("")}
          </span>
        </span>
        <span class="spool-group-weight">${remaining}g
          ${rank === 0 ? '<span class="spool-tag empty">Empty</span>' : rank === 1 ? '<span class="spool-tag low">Low</span>' : `<small>${pct}%</small>`}
        </span>
        <span class="spool-group-chev" aria-hidden="true">›</span>
      </button>
      <div class="spool-bar-wrap spool-group-bar"><div class="spool-bar-fill" style="width:${pct}%${rank === 0 ? ";background:var(--red)" : rank === 1 ? ";background:var(--yellow)" : ""}"></div></div>
      ${open ? `<div class="spool-group-reels">${g.spools.map(_spoolReelRow).join("")}</div>` : ""}
    </div>`;
  }).join("");
  list.querySelectorAll(".spool-group-head").forEach(btn => btn.addEventListener("click", () => {
    const key = btn.closest(".spool-group").dataset.key;
    if (_spoolView.open.has(key)) _spoolView.open.delete(key); else _spoolView.open.add(key);
    renderSpoolPanel();
  }));
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
    const locPrinter   = printerByLocation(loc);
    const otherPrinter = loc && locPrinter?.id !== printerId ? locPrinter : null;
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
  const loc = printerLocation(printerId);
  try {
    // Unassign existing spool on this printer if it's different
    const cur = spools.find(s => printerByLocation(spoolAssignedTo(s))?.id === printerId);
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
        body: JSON.stringify({ location: loc }),
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
  // Linking goes over the live connection to Spooler. Without it nothing is saved
  // (and Spoolman's location isn't set), so say so instead of reporting success.
  if (!(ws && ws.readyState === WebSocket.OPEN)) {
    toast("Not connected to Spooler — nothing was linked. Wait for the connection and try again.", true);
    return;
  }
  ws.send(JSON.stringify({
    action:     "link_tray",
    printer_id: printerId,
    tray_id:    trayId,
    spool_id:   spoolId,
  }));
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


// ─── Filament deductions waiting for Spoolman (T22) ───────────────────────────
// Spooler writes every deduction to a ledger before sending it; this shows the
// ones Spoolman hasn't taken (yet), lets you retry or discard them, and puts a
// badge on the Spools button when something needs a person.
let _ledgerItems = [];

function _ledgerStatusText(e) {
  if (e.status === "discarded") return `Not deducted — ${e.last_error || "discarded"}`;
  if (e.status === "sending") return "Sending…";
  if (e.status === "failed" && e.next_try_at == null) return "Waiting for your decision";
  if (e.status === "failed") return `Failed ${e.attempts}× — will try again ${_ledgerWhen(e.next_try_at)}`;
  return "Waiting to be sent";
}
function _ledgerWhen(epoch) {
  const s = Math.round(epoch - _serverNowS());
  if (s <= 5) return "shortly";
  if (s < 90) return `in ${s}s`;
  if (s < 5400) return `in ${Math.round(s / 60)} min`;
  return `in ${Math.round(s / 3600)} h`;
}

function renderLedger() {
  const card = document.getElementById("ledger-card");
  if (!card) return;
  // What Spoolman already has is done: only what is still waiting, failed or was discarded is listed.
  const shown = _ledgerItems.filter(e => e.status !== "sent");
  if (!shown.length) { card.hidden = true; card.replaceChildren(); return; }
  const unsent = shown.filter(e => ["pending", "sending", "failed"].includes(e.status));
  card.hidden = false;
  const head = _el("div", "ledger-head");
  head.append(_el("strong", null, unsent.length
    ? `${unsent.length} filament deduction${unsent.length === 1 ? "" : "s"} not yet in Spoolman`
    : "Recent filament that was not deducted"));
  const rows = shown.map(e => {
    const row = _el("div", `ledger-row ${e.status}`);
    const sp = e.spool_id != null ? (spools.find(x => x.id === e.spool_id) || null) : null;
    const spoolText = e.spool_id == null ? "unknown spool" : (sp ? spoolName(sp) : `spool #${e.spool_id}`);
    const main = _el("div", "ledger-main");
    main.append(_el("div", "ledger-title", `${_fmtNum(e.grams, 1)} g from ${spoolText}`));
    main.append(_el("div", "ledger-meta", `${e.printer_name || "Printer"}${e.filename ? " · " + e.filename : ""} · ${new Date(e.created_at * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}`));
    main.append(_el("div", "ledger-state", _ledgerStatusText(e)));
    if (e.status === "failed" && e.last_error) main.append(_el("div", "ledger-error", e.last_error));
    if (e.note) main.append(_el("div", "ledger-meta", e.note));
    row.append(main);
    if (["pending", "failed"].includes(e.status)) {
      const retry = _el("button", "btn btn-secondary btn-sm", "Try again now");
      retry.type = "button";
      retry.addEventListener("click", () => _ledgerAction(e.id, "retry"));
      const drop = _el("button", "btn btn-secondary btn-sm", "Discard");
      drop.type = "button";
      drop.addEventListener("click", () => {
        if (confirm(`Discard ${_fmtNum(e.grams, 1)} g from ${spoolText}? Spoolman will NOT be told about this filament.`)) _ledgerAction(e.id, "discard");
      });
      const act = _el("div", "ledger-actions");
      act.append(retry, drop);
      row.append(act);
    }
    if (e.status === "discarded") {
      const hide = _el("button", "btn btn-secondary btn-sm", "Dismiss");
      hide.type = "button";
      hide.addEventListener("click", () => _ledgerAction(e.id, "discard"));
      const act = _el("div", "ledger-actions");
      act.append(hide);
      row.append(act);
    }
    return row;
  });
  card.replaceChildren(head, ...rows);
}

async function _ledgerAction(id, action) {
  try {
    const r = await fetch(`/api/spoolman-ledger/${encodeURIComponent(id)}${action === "retry" ? "/retry" : ""}`,
                          { method: action === "retry" ? "POST" : "DELETE" });
    if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.error || "Could not do that", true); }
    else toast(action === "retry" ? "Trying again" : "Done");
  } catch (e) { toast("Could not do that: " + e.message, true); }
  loadLedger();
}

function _setLedgerBadge(summary) {
  const b = document.getElementById("spools-badge");
  if (!b) return;
  const n = summary?.attention || 0;
  b.hidden = n === 0;
  b.textContent = n ? String(n) : "";
}

async function loadLedger() {
  if (!featureEnabled("spoolman")) { _ledgerItems = []; renderLedger(); _setLedgerBadge(null); return; }
  try {
    const r = await fetch("/api/spoolman-ledger");
    if (!r.ok) return;
    const d = await r.json();
    _ledgerItems = d.items || [];
    _setLedgerBadge(d.summary);
    renderLedger();
  } catch (_) { /* server may not be ready yet */ }
}

// Spools side panel
const spoolsPanel = document.getElementById("panel-spools");
const btnSpools   = document.getElementById("btn-spools");

const openSpools = () => {
  spoolsPanel.classList.add("open");
  historyBackdrop.classList.add("open");
  btnSpools.classList.add("active");
  fetchSpools();
  loadLedger();
};
const closeSpools = () => {
  spoolsPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  btnSpools.classList.remove("active");
};

btnSpools.addEventListener("click", openSpools);
document.getElementById("btn-spools-close").addEventListener("click", closeSpools);

async function loadImportBrands() {
  const sel = document.getElementById("import-brand");
  if (!sel || sel.dataset.loaded) return;
  try {
    const r = await fetch("/api/filament-meta");
    if (!r.ok) throw new Error(r.status);
    const { brands } = await r.json();
    if (!brands.length) throw new Error("empty");
    sel.innerHTML = brands.map(b => `<option value="${escAttr(b)}">${escHtml(b)}</option>`).join("");
    const elegoo = brands.find(b => b.toLowerCase() === "elegoo");
    if (elegoo) sel.value = elegoo;
    sel.dataset.loaded = "1";
  } catch (_) {
    sel.innerHTML = '<option value="">Could not load brands</option>';
  }
}

document.getElementById("btn-import-filaments").addEventListener("click", async () => {
  const btn = document.getElementById("btn-import-filaments");
  const brand = document.getElementById("import-brand").value;
  if (!brand) { toast("Pick a brand first", true); return; }
  btn.disabled = true;
  btn.textContent = "Importing…";
  try {
    const r = await fetch("/api/import-filaments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brand }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    if (d.created > 0) {
      alert(`✓ Imported ${d.created} ${brand} filaments into Spoolman.\n\nYou can now create spools from these in Spoolman UI or when adding a spool here.`);
    } else {
      alert(`All ${d.total} ${brand} filaments are already in Spoolman (${d.skipped} skipped).`);
    }
  } catch (e) {
    alert("Import failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Import";
  }
});

// Close all panels on backdrop click
historyBackdrop.addEventListener("click", () => { closeSpools(); closeStats(); closePrinters(); });

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

