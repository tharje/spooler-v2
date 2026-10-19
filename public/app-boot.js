/* Spooler frontend: Changelog, demo mode, boot, report bug. Plain scripts sharing one global scope; load order is in index.html. */
"use strict";

// ─── Changelog ────────────────────────────────────────────────────────────────
let _changelog = [];

async function loadChangelog() {
  try {
    const r = await fetch("/changelog.json?v=" + Date.now());
    const data = await r.json();
    if (!Array.isArray(data) || data.length === 0) return;
    _changelog = data;
    const badge = document.getElementById("version-badge");
    if (badge) badge.textContent = "v" + _changelog[0].version;
  } catch (_) {}
}

function openChangelog() {
  const body = document.getElementById("changelog-body");
  if (!body) return;
  body.innerHTML = _changelog.map(entry => `
    <div class="cl-entry">
      <div class="cl-entry-header">
        <span class="cl-version">v${entry.version}</span>
        <span class="cl-date">${entry.date}</span>
      </div>
      <ul class="cl-list">
        ${entry.changes.map(c => `<li>${escHtml(c)}</li>`).join("")}
      </ul>
    </div>`).join("");
  document.getElementById("modal-changelog")?.classList.add("open");
}

document.getElementById("version-badge")?.addEventListener("click", openChangelog);
document.getElementById("btn-changelog-close")?.addEventListener("click", () =>
  document.getElementById("modal-changelog")?.classList.remove("open"));
document.getElementById("modal-changelog")?.addEventListener("click", e => {
  if (e.target.id === "modal-changelog")
    e.target.classList.remove("open");
});

// ─── Demo mode (?demo=states) ───────────────────────────────────────────────────
// One synthetic card per state, side by side — lets anyone sanity-check every
// status dot/badge/reason-box combination at once without needing a printer
// in every possible state. No WebSocket connection is made in this mode.
function renderDemoStates() {
  const reasonFor = (s) => {
    if (s === "error") return { kind: "error", initiated_by: "printer", code: "99",
                                 category: "unknown", message: "Demo error message" };
    if (s === "pausing" || s === "paused") return { kind: "pause", initiated_by: "unknown",
                                 code: "", category: "unknown", message: "" };
    if (s === "cancelled" || s === "stopping") return { kind: "stop", initiated_by: "spooler",
                                 code: "", category: "unknown", message: "" };
    return null;
  };
  Object.keys(STATE_LABEL).forEach((s) => {
    const p = {
      id: `demo-${s}`, ip: "demo", name: `Demo: ${STATE_LABEL[s]}`,
      printer_type: "cc1", connected: s !== "offline",
      state: s, state_reason: reasonFor(s),
      status: { PrintInfo: { Filename: "demo.gcode", CurrentLayer: 10, TotalLayer: 100,
                              PrintTime: 600, RemainTime: 900, TotalExtrusion: 1200 } },
      attrs: {}, camera_url: null, filament_mm: 1200, filament_g: 3.6, has_access_code: false,
    };
    printers[p.id] = p;
    renderPrinter(p);
  });
}

// Mobile browsers (notably when installed as a PWA) suspend the network
// connection behind an MJPEG <img> while the app is backgrounded, and the
// stream doesn't resume on its own since the <img> src never changes -- it
// hangs showing the last frame (or the alt text) until something forces a
// fresh request. Re-point every camera <img> at a cache-busted URL whenever
// the app comes back to the foreground to force that reconnect.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  document.querySelectorAll('.card-camera img').forEach(img => {
    if (!img._baseUrl) return;
    const sep = img._baseUrl.includes('?') ? '&' : '?';
    img.src = `${img._baseUrl}${sep}_r=${Date.now()}`;
  });
});

// ─── Boot ──────────────────────────────────────────────────────────────────────
if (new URLSearchParams(location.search).get("demo") === "states") {
  renderDemoStates();
} else {
  fetch("/api/auth-status")
    .then(r => r.json())
    .then(({ spoolman_url }) => {
      if (spoolman_url) document.getElementById("btn-spoolman-ui").href = spoolman_url;
    })
    .catch(() => {});
  loadChangelog();
  loadFeatures();
  loadIntegrations();
  connect();
  // Staleness is purely a function of wall-clock time passing, not of new
  // data arriving — a card can go stale with no new printer_update at all,
  // so it needs its own tick independent of the WS message flow.
  setInterval(() => Object.values(printers).forEach(renderPrinter), 10000);
}

// On startup: if notifications are enabled but the subscription was cleared by the
// browser (e.g. after cache purge), silently re-subscribe so notifications keep working.
(async () => {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  try {
    const resp = await fetch("/api/notification-settings");
    if (!resp.ok) return;
    const s = await resp.json();
    if (!s || !Object.values(s).some(v => v?.enabled)) return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) await _subscribePush();
  } catch (_) {}
})();


// ─── Report problem ───────────────────────────────────────────────────────────
const _ISSUES_URL = "https://github.com/tharje/spooler-v2/issues/new";

const _URL_BUDGET = 7000; // GitHub rejects very long prefilled URLs

function _reportEnv(printerId) {
  const version = (typeof _changelog !== "undefined" && _changelog[0]) ? _changelog[0].version : "unknown";
  const list = printerId === "none" ? [] : Object.entries(printers).filter(([id]) => !printerId || id === printerId).map(([, p]) => p);
  const lines = list.map(p =>
    `- ${p.printer_type}${p.attrs?.Model ? " " + p.attrs.Model : ""}` +
    `${p.attrs?.FirmwareVersion ? ", firmware " + p.attrs.FirmwareVersion : ""}`);
  return [
    "**Environment**",
    `- Spooler version: ${version}`,
    `- Browser: ${navigator.userAgent}`,
    "- Printer:", ...(lines.length ? lines : ["- (none)"]),
  ].join("\n");
}

// Build title + body, trimming the oldest log lines until the URL fits.
function _reportIssueUrl() {
  const printerId = document.getElementById("report-printer").value;
  const title = document.getElementById("report-title").value.trim();
  const desc = document.getElementById("report-desc").value.trim();
  const diag = document.getElementById("report-diag").value;
  const head = `${desc || "_(no description)_"}\n\n${_reportEnv(printerId)}\n\n<details><summary>Diagnostics</summary>\n\n\`\`\`\n`;
  const tail = "\n```\n</details>\n";
  const make = (d) => `${_ISSUES_URL}?title=${encodeURIComponent("[Bug] " + title)}&body=${encodeURIComponent(head + d + tail)}`;
  let lines = diag.split("\n"), url = make(lines.join("\n")), cut = false;
  while (url.length > _URL_BUDGET && lines.length > 20) {
    lines = lines.slice(Math.ceil(lines.length * 0.15) || 1);
    cut = true;
    url = make("(older lines trimmed to fit - use Download to attach the full report)\n" + lines.join("\n"));
  }
  return url;
}

async function _loadDiagnostics() {
  const box = document.getElementById("report-diag");
  const id = document.getElementById("report-printer").value;
  box.value = "Loading…";
  try {
    const r = await fetch("/api/diagnostics?printer=" + encodeURIComponent(id));
    box.value = r.ok ? await r.text() : `Could not load diagnostics (HTTP ${r.status}).`;
  } catch (e) {
    box.value = "Could not load diagnostics: " + e;
  }
}

async function openReport() {
  const sel = document.getElementById("report-printer");
  const entries = Object.entries(printers);
  sel.innerHTML = "";
  if (entries.length > 1) sel.add(new Option("All printers", ""));
  entries.forEach(([id, p]) => sel.add(new Option(p.name || id, id)));
  sel.add(new Option("Not printer-related", "none"));
  document.getElementById("report-printer-row").hidden = sel.options.length <= 2;
  document.getElementById("report-title").value = "";
  document.getElementById("report-desc").value = "";
  document.getElementById("modal-report").classList.add("open");
  await _loadDiagnostics();
}

function closeReport() { document.getElementById("modal-report").classList.remove("open"); }

document.getElementById("btn-report-problem")?.addEventListener("click", openReport);
document.getElementById("btn-report-close")?.addEventListener("click", closeReport);
document.getElementById("modal-report")?.addEventListener("click", (e) => {
  if (e.target === document.getElementById("modal-report")) closeReport();
});
document.getElementById("report-printer")?.addEventListener("change", _loadDiagnostics);
document.getElementById("btn-report-github")?.addEventListener("click", () => {
  window.open(_reportIssueUrl(), "_blank", "noopener");
});
document.getElementById("btn-report-copy")?.addEventListener("click", async () => {
  const box = document.getElementById("report-diag");
  try { await navigator.clipboard.writeText(box.value); }
  catch (_) { box.select(); document.execCommand("copy"); }
  toast("Diagnostics copied");
});
document.getElementById("btn-report-download")?.addEventListener("click", () => {
  const blob = new Blob([document.getElementById("report-diag").value], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "spooler-diagnostics.txt";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});
