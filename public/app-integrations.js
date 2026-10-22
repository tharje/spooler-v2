/* Spooler frontend: API access, login warning, feature flags, integrations. Plain scripts sharing one global scope; load order is in index.html. */
"use strict";

// ─── History panel ────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const r = await fetch("/api/history");
    history = (await r.json()).reverse(); // newest first
    renderHistory();
  } catch (e) { /* server may not be ready yet */ }
}


// ─── API access (keys for other programs) ────────────────────────────────────
let _apiTokens = [];

async function _loadApiPage() {
  document.getElementById("api-address").textContent = `${location.origin}/api/external/v1/`;
  document.getElementById("api-enabled").checked = featureEnabled("external_api");
  document.getElementById("api-new-key").hidden = true;
  try {
    const r = await fetch("/api/api-tokens");
    if (r.ok) _apiTokens = (await r.json()).tokens || [];
  } catch (_) { /* keep the previous list */ }
  _renderApiTokens();
}

function _renderApiTokens() {
  const host = document.getElementById("api-token-list");
  if (!_apiTokens.length) {
    host.replaceChildren(_el("p", "stats-empty", "No keys yet."));
    return;
  }
  const rows = _apiTokens.map(t => {
    const row = _el("div", "api-key-row");
    const info = _el("div", "api-key-info");
    info.append(_el("strong", null, t.name));
    info.append(_el("span", "api-key-meta",
      `${t.scope === "write" ? "Read + change references" : "Read only"} · ${t.hint} · created ${_fmtWhen(t.created).slice(0, 10)} · ` +
      (t.last_used ? `last used ${_fmtWhen(t.last_used)}` : "never used")));
    const del = _el("button", "btn btn-secondary btn-sm", "Revoke");
    del.type = "button";
    del.addEventListener("click", async () => {
      if (!confirm(`Revoke "${t.name}"? Any program using this key stops working.`)) return;
      const r = await fetch(`/api/api-tokens/${encodeURIComponent(t.id)}`, { method: "DELETE" });
      if (r.ok) { toast("Key revoked"); _loadApiPage(); } else toast("Could not revoke the key", true);
    });
    row.append(info, del);
    return row;
  });
  host.replaceChildren(...rows);
}

document.getElementById("btn-settings-goto-api")?.addEventListener("click", () => {
  _showSettingsPage(_settingsApiPage);
  _loadApiPage();
});
document.getElementById("btn-settings-back-api")?.addEventListener("click", _backToSettingsMenu);

document.getElementById("api-enabled")?.addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  if (enabled && !confirm("Allow other programs to read your print history and pictures with an API key? Keys are created below; nothing is reachable without one.")) {
    e.target.checked = false;
    return;
  }
  try {
    const r = await fetch("/api/features", { method: "PATCH", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ key: "external_api", enabled }) });
    const d = await r.json();
    if (!r.ok) { toast(d.error || "Could not change this", true); e.target.checked = !enabled; return; }
    _applyFeatures(d.features);
    toast(enabled ? "API access is on" : "API access is off");
  } catch (err) { toast("Could not change this: " + err.message, true); e.target.checked = !enabled; }
});

document.getElementById("btn-api-create")?.addEventListener("click", async () => {
  const name = document.getElementById("api-key-name").value.trim();
  const scope = document.getElementById("api-key-scope").value;
  const r = await fetch("/api/api-tokens", { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify({ name, scope }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || "Could not create the key", true); return; }
  document.getElementById("api-key-name").value = "";
  const box = document.getElementById("api-new-key");
  const code = _el("code", null, d.key);
  const copy = _el("button", "btn btn-secondary btn-sm", "Copy");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(d.key); toast("Key copied"); }
    catch (_) { const sel = getSelection(); const rng = document.createRange(); rng.selectNodeContents(code); sel.removeAllRanges(); sel.addRange(rng); toast("Select and copy the key"); }
  });
  box.replaceChildren(_el("strong", null, `Key for "${d.token.name}" — copy it now`),
                      _el("p", "report-desc", "This is the only time it is shown. If you lose it, revoke it and create a new one."),
                      code, copy);
  box.hidden = false;
  if (!featureEnabled("external_api")) toast("The key is created, but API access is still switched off above", true);
  _apiTokens.push(d.token);
  _renderApiTokens();
});


// ─── Login-off warning ───────────────────────────────────────────────────────
// With AUTH_ENABLED=false anyone on the network can use Spooler; say so, for as
// long as that is the case (footer, and on the Features page).
async function loadSecurityStatus() {
  try {
    const r = await fetch("/api/security-status");
    if (!r.ok) return;
    const { auth_enabled } = await r.json();
    document.getElementById("auth-warning").hidden = auth_enabled !== false;
    document.getElementById("features-auth-warning").hidden = auth_enabled !== false;
  } catch (_) { /* server may not be ready yet */ }
}
document.getElementById("auth-warning")?.addEventListener("click", (e) => {
  e.preventDefault();
  _openSettings();
  _showSettingsPage(_settingsFeaturesPage);
  _renderFeaturesList();
});

// ─── Feature flags ───────────────────────────────────────────────────────────
async function loadFeatures() {
  try {
    const r = await fetch("/api/features");
    if (r.ok) _applyFeatures(await r.json());
  } catch (e) { /* server may not be ready yet */ }
}

// ─── Integrations config (T8) ────────────────────────────────────────────────
let integrations = { fields: [], tests: {}, server_settings: {} };

function integrationField(key) {
  return integrations.fields.find(f => f.key === key);
}

async function loadIntegrations() {
  try {
    const r = await fetch("/api/integrations");
    if (!r.ok) return;
    integrations = await r.json();
    const slicerUrl = integrationField("slicer.url")?.value || "";
    const slicerBtn = document.getElementById("btn-slicer");
    if (slicerBtn) {
      slicerBtn.hidden = !slicerUrl;
      if (slicerUrl) slicerBtn.href = slicerUrl;
    }
    if (_settingsIntegrationsPage && _settingsIntegrationsPage.style.display !== "none") {
      _renderIntegrationsList();
    }
  } catch (e) { /* server may not be ready yet */ }
}

const _INTEGRATION_GROUPS = [
  { title: "Spoolman", prefix: "spoolman.", testKey: "spoolman" },
  { title: "Slicer",   prefix: "slicer.",   testKey: null },
  { title: "Spaghetti detection (Obico)", prefix: "spaghetti.", testKey: "spaghetti",
    help: "Run Obico's ML API as its own container, give its address here, and the address Spooler has as seen from that container (for example http://spooler:8080 in the same Docker network). Then turn on Spaghetti detection under Features and 'Possible print failure' under Notifications." },
  { title: "Picture collection", prefix: "dataset.", testKey: null,
    help: "Saves camera pictures during prints to a folder you choose. Spooler's developer is considering building a print-failure (spaghetti) detector made specifically for Elegoo Centauri printers, and these pictures could help train it. Nothing is ever sent automatically: the pictures stay on your own machine, and you decide whether to share some. If you would like to, open an issue on Spooler's GitHub page and we will work out how. Turn it on under Features. While a print runs, each printer card gets two buttons (Looks OK / Failing now) to mark what you see; marked pictures are the most useful. In Docker, choose a folder that is mounted from the host (for example add - /home/you/print-dataset:/dataset under volumes and enter /dataset here), otherwise the pictures are lost when the container is rebuilt." },
];

// Static markup in index.html; re-attached into the Spoolman group after each render.
const _catalogueEl = document.querySelector(".integrations-catalogue");
const _openIntegrationGroups = new Set();   // groups the user has opened, kept across re-renders (e.g. after "Test connection")

function _renderIntegrationsList() {
  const container = document.getElementById("integrations-list");
  if (!container) return;

  container.innerHTML = _INTEGRATION_GROUPS.map(group => {
    const fields = integrations.fields.filter(f => f.key.startsWith(group.prefix));
    if (!fields.length) return "";
    const test = group.testKey ? integrations.tests[group.testKey] : null;

    const fieldsHtml = fields.map(f => {
      const sourceLabel = f.source === "env" ? "from environment variable"
                         : f.source === "ui"  ? "custom"
                         : "default";
      const lockedNote = f.locked
        ? `<div class="integration-locked-note">Locked by server configuration</div>` : "";

      if (f.type === "bool") {
        return `
          <div class="integration-field">
            <div style="display:flex;align-items:center;gap:10px;justify-content:space-between">
              <span>${escHtml(f.label)}</span>
              <label class="toggle-switch">
                <input type="checkbox" data-config-key="${escAttr(f.key)}"
                       ${f.value ? "checked" : ""} ${f.locked ? "disabled" : ""} />
                <span class="toggle-slider"></span>
              </label>
            </div>
            <div class="integration-field-meta"><span class="integration-source-badge">${escHtml(sourceLabel)}</span></div>
            ${lockedNote}
          </div>`;
      }

      const inputType = f.type === "secret" ? "password" : f.type === "int" ? "number" : "text";
      const placeholder = f.type === "secret" ? (f.set ? "•••• (leave blank to keep)" : "Not set") : "";
      const value = f.type === "secret" ? "" : escAttr(f.value ?? "");
      const removeBtn = (f.type === "secret" && f.set)
        ? `<button type="button" class="btn btn-secondary btn-sm" data-clear-key="${escAttr(f.key)}">Remove</button>` : "";
      return `
        <div class="integration-field">
          <label>${escHtml(f.label)}
            <input type="${inputType}" data-config-key="${escAttr(f.key)}" value="${value}"
                   placeholder="${escAttr(placeholder)}" ${f.locked ? "disabled" : ""} />
          </label>
          <div class="integration-field-meta">
            <span class="integration-source-badge">${escHtml(sourceLabel)}</span>
            ${removeBtn}
          </div>
          ${lockedNote}
        </div>`;
    }).join("");

    const testHtml = group.testKey ? `
      <div class="integration-test-row">
        <button type="button" class="btn btn-secondary btn-sm" data-test-key="${escAttr(group.testKey)}">Test connection</button>
        ${test ? `<span class="integration-test-result ${test.ok ? "ok" : "fail"}">${escHtml(test.message)}</span>` : ""}
      </div>` : "";

    return `
      <details class="integration-group notif-group notif-group-events" data-integration-group="${escAttr(group.title)}" ${_openIntegrationGroups.has(group.title) ? "open" : ""}>
        <summary>
          <span class="notif-group-text"><strong>${escHtml(group.title)}</strong></span>
          <svg class="notif-group-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M9 18l6-6-6-6"/></svg>
        </summary>
        <div class="integration-group-body notif-group-body">${group.help ? `<p class="report-desc">${escHtml(group.help)}</p>` : ""}${fieldsHtml}${testHtml}</div>
      </details>`;
  }).join("");

  container.querySelectorAll("details[data-integration-group]").forEach(d => {
    d.addEventListener("toggle", () => {
      d.open ? _openIntegrationGroups.add(d.dataset.integrationGroup) : _openIntegrationGroups.delete(d.dataset.integrationGroup);
    });
  });

  const spoolmanBody = container.querySelector(".integration-group .integration-group-body");
  if (_catalogueEl && spoolmanBody && container.querySelector(".integration-group summary strong")?.textContent === "Spoolman") {
    spoolmanBody.appendChild(_catalogueEl);
  }

  container.querySelectorAll("button[data-test-key]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.testKey;
      btn.disabled = true;
      btn.textContent = "Testing…";
      try {
        const r = await fetch(`/api/integrations/${encodeURIComponent(key)}/test`, { method: "POST" });
        integrations.tests[key] = await r.json();
      } catch (e) {
        integrations.tests[key] = { ok: false, message: e.message };
      }
      _renderIntegrationsList();
    });
  });

  container.querySelectorAll("button[data-clear-key]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.clearKey;
      try {
        const r = await fetch("/api/integrations", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ clear: [key] }),
        });
        const data = await r.json();
        if (!r.ok) { toast(data.error || "Could not clear field", true); return; }
        integrations.fields = data.fields;
        _renderIntegrationsList();
        toast("Cleared");
      } catch (e) {
        toast("Could not clear field: " + e.message, true);
      }
    });
  });
}

function _renderServerSettings() {
  const el = document.getElementById("integrations-server-settings-list");
  if (!el) return;
  const s = integrations.server_settings || {};
  el.innerHTML = Object.entries(s)
    .map(([k, v]) => `<span>${escHtml(k)}</span><b>${escHtml(String(v))}</b>`)
    .join("");
}

document.getElementById("btn-integrations-save")?.addEventListener("click", async () => {
  const values = {};
  document.querySelectorAll("#integrations-list [data-config-key]").forEach(input => {
    const key = input.dataset.configKey;
    values[key] = input.type === "checkbox" ? input.checked : input.value;
  });
  try {
    const r = await fetch("/api/integrations", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const data = await r.json();
    if (!r.ok) {
      toast(data.error || "Could not save", true);
      return;
    }
    toast("Saved");
    await loadIntegrations();
    _renderIntegrationsList();
  } catch (e) {
    toast("Could not save: " + e.message, true);
  }
});

// Settings menu items (and their sub-pages) that hide entirely when their
// feature is off, instead of just rejecting on the server -- so the user
// never sees a settings entry for something that's been switched off.
const _GATED_SETTINGS_PAGES = [
  { feature: "backup",        btnId: "btn-settings-goto-backup",        page: () => _settingsBackupPage },
  { feature: "notifications", btnId: "btn-settings-goto-notifications", page: () => _settingsNotifPage },
];

function _applyFeatures(list) {
  features = {};
  list.forEach(f => { features[f.key] = f; });
  Object.values(printers).forEach(renderPrinter);
  if (_currentFilePrinterId) updateUploadUi();
  const reportBtn = document.getElementById("btn-report-problem");
  if (reportBtn) reportBtn.style.display = featureEnabled("report_problem") ? "" : "none";
  const statsBtn = document.getElementById("btn-stats");
  if (statsBtn) statsBtn.style.display = featureEnabled("statistics") ? "" : "none";
  const serviceBtn = document.getElementById("btn-service");
  if (serviceBtn) serviceBtn.style.display = featureEnabled("service_log") ? "" : "none";
  const queueBtn = document.getElementById("btn-queue");
  if (queueBtn) queueBtn.style.display = featureEnabled("job_queue") ? "" : "none";
  const spoolsBtn = document.getElementById("btn-spools");
  if (spoolsBtn) spoolsBtn.style.display = featureEnabled("spoolman") ? "" : "none";

  _GATED_SETTINGS_PAGES.forEach(({ feature, btnId, page }) => {
    const btn = document.getElementById(btnId);
    const enabled = featureEnabled(feature);
    if (btn) btn.style.display = enabled ? "" : "none";
    // If that page is open when its feature gets turned off (e.g. toggled
    // from another tab), don't leave the user stranded on a now-hidden page.
    const pageEl = page();
    if (!enabled && pageEl && pageEl.style.display !== "none") {
      _backToSettingsMenu();
    }
  });

  if (_settingsFeaturesPage && _settingsFeaturesPage.style.display !== "none") {
    _renderFeaturesList();
  }
}

const _FEATURE_GROUPS = [
  { title: "Monitoring",    keys: ["camera", "print_snapshot", "spaghetti_detection", "dataset_collection"] },
  { title: "Notifications", keys: ["notifications"] },
  { title: "Data",          keys: ["backup", "statistics", "service_log"] },
  { title: "Printing",      keys: ["job_queue"] },
  { title: "Services",      keys: ["spoolman", "update_check"] },
];

const _openFeatureGroups = new Set();   // menus the user has opened, kept across re-renders

function _renderFeaturesList() {
  const container = document.getElementById("features-list");
  if (!container) return;
  container.innerHTML = _FEATURE_GROUPS.map(group => {
    const rows = group.keys.filter(k => features[k]).map(key => {
      const f = features[key];
      const lockedNote = f.locked
        ? `<div class="feature-locked-note">${escHtml(f.lock_reason || "Locked off by server configuration")}</div>`
        : "";
      return `
        <div class="feature-row">
          <div class="feature-info">
            <span class="feature-label">${escHtml(f.name)}</span>
            <span class="feature-desc">${escHtml(f.description)}</span>
            ${lockedNote}
          </div>
          <label class="toggle-switch">
            <input type="checkbox" data-feature-key="${escAttr(key)}"
                   ${f.enabled ? "checked" : ""} ${f.locked ? "disabled" : ""} />
            <span class="toggle-slider"></span>
          </label>
        </div>`;
    }).join("");
    if (!rows) return "";
    const present = group.keys.filter(k => features[k]);
    const on = present.filter(k => features[k].enabled).length;
    return `
      <details class="notif-group notif-group-events" data-feature-group="${escAttr(group.title)}" ${_openFeatureGroups.has(group.title) ? "open" : ""}>
        <summary>
          <span class="notif-group-text"><strong>${escHtml(group.title)}</strong></span>
          <span class="notif-group-pill">${on ? `${on} of ${present.length} on` : "none on"}</span>
          <svg class="notif-group-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M9 18l6-6-6-6"/></svg>
        </summary>
        <div class="notif-group-body">${rows}</div>
      </details>`;
  }).join("");

  container.querySelectorAll("details[data-feature-group]").forEach(d => {
    d.addEventListener("toggle", () => {
      d.open ? _openFeatureGroups.add(d.dataset.featureGroup) : _openFeatureGroups.delete(d.dataset.featureGroup);
    });
  });

  container.querySelectorAll("input[data-feature-key]").forEach(input => {
    input.addEventListener("change", async () => {
      const key = input.dataset.featureKey;
      const enabled = input.checked;
      if (enabled && features[key]?.risky && !confirm(`${features[key].name} is marked risky. Enable it?`)) {
        input.checked = false;
        return;
      }
      try {
        const r = await fetch("/api/features", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key, enabled }),
        });
        const data = await r.json();
        if (!r.ok) {
          toast(data.error || "Could not update feature", true);
          input.checked = !enabled;
          return;
        }
        _applyFeatures(data.features);
      } catch (e) {
        toast("Could not update feature: " + e.message, true);
        input.checked = !enabled;
      }
    });
  });
}

// The print list lives in the Stats panel ("Prints" tab); see renderPrints().
function renderHistory() { renderPrints(); }

// Full-size picture of a finished print (click or Esc to close).
function openSnapshot(id) {
  closeSnapshot();
  const box = document.createElement("div");
  box.id = "snap-lightbox";
  box.className = "snap-lightbox";
  box.innerHTML = `<img src="/api/snapshot/${escAttr(id)}" alt="Picture of the finished print" />`;
  box.addEventListener("click", closeSnapshot);
  document.body.appendChild(box);
}
function closeSnapshot() { document.getElementById("snap-lightbox")?.remove(); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSnapshot(); });

const historyBackdrop = document.getElementById("panel-backdrop");   // shared dimmer behind side panels
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSpools(); closeStats(); closePrinters(); closeFileBrowser(); closePrintOpts(); } });


