/* Spooler frontend: Statistics and print list. Plain scripts sharing one global scope; load order is in index.html. */
"use strict";

// ─── Statistics (T14) ─────────────────────────────────────────────────────────
// Numbers come from /api/stats (stats.py); this only draws them. The chart is
// inline SVG: single series, so the app accent is the one mark colour, bars are
// capped at 24px with a 4px rounded top, grid lines are hairlines, and every
// value is also reachable through the tooltip and the table view.
const statsPanel = document.getElementById("panel-stats");
const btnStats   = document.getElementById("btn-stats");
let _statsRange  = "30";
let _statsMetric = "grams";
let _statsView   = "chart";
let _statsTab    = "overview";     // "overview" | "prints"
let _openPrintId = null;           // the print whose detail view is open, if any
let _statsData   = null;

const _STATS_METRICS = {
  grams:   { label: "Filament (g)", unit: "g",  fmt: v => _fmtNum(v, v < 10 ? 1 : 0) },
  prints:  { label: "Prints",       unit: "",   fmt: v => _fmtNum(v, 0) },
  hours:   { label: "Print time (h)", unit: "h", fmt: v => _fmtNum(v, v < 10 ? 1 : 0) },
};

function _fmtNum(v, digits = 0) {
  return Number(v ?? 0).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits });
}
function _isoDay(d) {
  const z = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
}
function _statsPeriod() {
  if (_statsRange === "custom") {
    return { from: document.getElementById("stats-from").value, to: document.getElementById("stats-to").value };
  }
  if (_statsRange === "all") return { from: "", to: "" };
  const to = new Date(), from = new Date();
  if (_statsRange === "365") { from.setMonth(from.getMonth() - 11); from.setDate(1); }
  else from.setDate(from.getDate() - (parseInt(_statsRange, 10) - 1));
  return { from: _isoDay(from), to: _isoDay(to) };
}
function _statsQuery() {
  const { from, to } = _statsPeriod();
  const q = new URLSearchParams();
  if (from) q.set("from", from);
  if (to) q.set("to", to);
  const pr = document.getElementById("stats-printer").value;
  if (pr) q.set("printer", pr);
  return q.toString();
}

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;     // names come from files/printers: never innerHTML
  return e;
}

async function loadStats() {
  const body = document.getElementById("stats-body");
  body.style.opacity = _statsData ? ".5" : "1";      // keep the previous frame while reloading
  const q = _statsQuery();
  document.getElementById("btn-stats-csv").href = "/api/history.csv" + (q ? "?" + q : "");
  try {
    const r = await fetch("/api/stats" + (q ? "?" + q : ""));
    if (!r.ok) throw new Error(r.status);
    _statsData = await r.json();
    renderStats();
  } catch (e) {
    body.replaceChildren(_el("p", "stats-empty", "Could not load statistics."));
  } finally {
    body.style.opacity = "1";
  }
}

function _fmtHours(h) {
  return h >= 100 ? `${_fmtNum(h, 0)} h` : `${_fmtNum(h, 1)} h`;
}

function _statTile(label, value, sub) {
  const t = _el("div", "stat-tile");
  t.append(_el("div", "stat-tile-label", label), _el("div", "stat-tile-value", value));
  if (sub) t.append(_el("div", "stat-tile-sub", sub));
  return t;
}

function _niceStep(rough) {
  const exp = Math.pow(10, Math.floor(Math.log10(rough)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= rough) return m * exp;
  return 10 * exp;
}

function _bucketLabel(key, kind) {
  const d = new Date(kind === "month" ? key + "-01T00:00" : key + "T00:00");
  return kind === "month"
    ? d.toLocaleDateString(undefined, { month: "short", year: "2-digit" })
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
function _bucketTitle(key, kind) {
  const d = new Date(kind === "month" ? key + "-01T00:00" : key + "T00:00");
  if (kind === "month") return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const label = d.toLocaleDateString(undefined, { weekday: kind === "day" ? "short" : undefined, day: "numeric", month: "short", year: "numeric" });
  return kind === "week" ? `Week of ${label}` : label;
}

const _SVGNS = "http://www.w3.org/2000/svg";
function _svg(tag, attrs) {
  const e = document.createElementNS(_SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}

function _renderStatsChart(d, tip) {
  const metric = _STATS_METRICS[_statsMetric];
  const series = d.series;
  const W = 640, H = 230, L = 44, R = 8, T = 10, B = 26;
  const pw = W - L - R, ph = H - T - B;
  const vals = series.map(b => b[_statsMetric]);
  const max = Math.max(0, ...vals);
  const step = max > 0 ? _niceStep(max / 4) : 1;
  const ymax = Math.max(step, step * Math.ceil(max / step));
  const y = v => T + ph - (v / ymax) * ph;

  const svg = _svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "stats-chart", role: "group",
                            "aria-label": `${metric.label} per ${d.bucket}` });
  for (let v = 0; v <= ymax + 1e-9; v += step) {
    svg.append(_svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid" }));
    const t = _svg("text", { x: L - 6, y: y(v) + 4, class: "axis", "text-anchor": "end" });
    t.textContent = _fmtNum(v, step < 1 ? 1 : 0);
    svg.append(t);
  }
  const n = series.length, slot = pw / Math.max(1, n);
  const bw = Math.min(24, Math.max(2, slot * 0.62));
  const every = Math.max(1, Math.ceil(52 / slot));
  const hits = [];
  series.forEach((b, i) => {
    const cx = L + slot * i + slot / 2;
    const v = b[_statsMetric];
    if (v > 0) {
      const h = Math.max(2, (v / ymax) * ph), x = cx - bw / 2, top = T + ph - h, r = Math.min(4, h, bw / 2);
      svg.append(_svg("path", { class: "bar", "data-i": i,
        d: `M${x},${T + ph} V${top + r} a${r},${r} 0 0 1 ${r},${-r} H${x + bw - r} a${r},${r} 0 0 1 ${r},${r} V${T + ph} Z` }));
    }
    if (i % every === 0) {
      const lt = _svg("text", { x: cx, y: H - 8, class: "axis", "text-anchor": "middle" });
      lt.textContent = _bucketLabel(b.key, d.bucket);
      svg.append(lt);
    }
    // Hit area: the whole slot, full height -- never just the painted bar.
    const hit = _svg("rect", { x: L + slot * i, y: T, width: slot, height: ph, class: "hit", tabindex: 0,
      role: "img", "aria-label": `${_bucketTitle(b.key, d.bucket)}: ${b.prints} prints, ${_fmtNum(b.grams, 1)} g, ${_fmtNum(b.hours, 1)} h` });
    hits.push(hit);
    const show = () => {
      svg.querySelectorAll(".bar.hover").forEach(e => e.classList.remove("hover"));
      svg.querySelector(`.bar[data-i="${i}"]`)?.classList.add("hover");
      tip.replaceChildren(_el("div", "tip-title", _bucketTitle(b.key, d.bucket)));
      for (const [lab, val] of [["Filament", `${_fmtNum(b.grams, 1)} g`], ["Prints", _fmtNum(b.prints)], ["Print time", `${_fmtNum(b.hours, 1)} h`]]) {
        const row = _el("div", "tip-row");
        row.append(_el("strong", null, val), _el("span", null, lab));
        tip.append(row);
      }
      tip.hidden = false;
      const wrap = svg.parentElement, box = wrap.getBoundingClientRect(), sv = svg.getBoundingClientRect();
      const px = (cx / W) * sv.width + (sv.left - box.left);
      tip.style.left = Math.min(Math.max(px - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth) + "px";
      tip.style.top = "0px";
    };
    const hide = () => { tip.hidden = true; svg.querySelectorAll(".bar.hover").forEach(e => e.classList.remove("hover")); };
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", show);
    hit.addEventListener("blur", hide);
  });
  hits.forEach(h => svg.append(h));
  return svg;
}

function _statsTable(d) {
  const tbl = _el("table", "stats-table");
  const head = _el("tr");
  ["Period", "Prints", "Filament (g)", "Print time (h)"].forEach(t => head.append(_el("th", null, t)));
  const thead = _el("thead");
  thead.append(head);
  tbl.append(thead);
  const tb = _el("tbody");
  d.series.forEach(b => {
    const tr = _el("tr");
    tr.append(_el("td", null, _bucketTitle(b.key, d.bucket)), _el("td", null, _fmtNum(b.prints)),
              _el("td", null, _fmtNum(b.grams, 1)), _el("td", null, _fmtNum(b.hours, 1)));
    tb.append(tr);
  });
  tbl.append(tb);
  return tbl;
}

function _barList(title, rows, unit) {
  const card = _el("div", "stats-card");
  card.append(_el("h4", null, title));
  if (!rows.length) { card.append(_el("p", "stats-empty", "Nothing yet")); return card; }
  const max = Math.max(...rows.map(r => r.value), 1);
  rows.forEach(r => {
    const row = _el("div", "bar-row");
    const top = _el("div", "bar-row-top");
    top.append(_el("span", "bar-row-label", r.label), _el("strong", null, r.text));
    const track = _el("div", "bar-track");
    const fill = _el("div", "bar-fill");
    fill.style.width = Math.max(2, (r.value / max) * 100) + "%";
    track.append(fill);
    row.append(top, track);
    if (r.sub) row.append(_el("div", "bar-row-sub", r.sub));
    card.append(row);
  });
  return card;
}

function renderStats() {
  const d = _statsData, body = document.getElementById("stats-body");
  if (!d) return;
  const frag = document.createDocumentFragment();

  if (!d.prints) {
    frag.append(_el("p", "stats-empty", "No prints in this period."));
    body.replaceChildren(frag);
    return;
  }

  const tiles = _el("div", "stat-tiles");
  tiles.append(
    _statTile("Prints", _fmtNum(d.prints), `${d.results.complete} done · ${d.results.cancelled} stopped · ${d.results.error} failed`),
    _statTile("Success rate", d.success_rate == null ? "—" : `${_fmtNum(d.success_rate, 0)}%`, "finished without being stopped"),
    _statTile("Print time", _fmtHours(d.hours), d.avg_print_s ? `avg ${formatTime(d.avg_print_s)} per finished print` : ""),
    _statTile("Filament", d.grams >= 1000 ? `${_fmtNum(d.grams / 1000, 2)} kg` : `${_fmtNum(d.grams, 0)} g`, "includes stopped prints"),
  );
  frag.append(tiles);

  // Chart card: metric switch + chart/table switch
  const card = _el("div", "stats-card stats-chart-card");
  const bar = _el("div", "stats-chart-bar");
  bar.append(_el("h4", null, `${_STATS_METRICS[_statsMetric].label} per ${d.bucket}`));
  const seg = _el("div", "seg seg-sm");
  Object.entries(_STATS_METRICS).forEach(([k, m]) => {
    const b = _el("button", k === _statsMetric ? "active" : "", k === "grams" ? "Filament" : k === "prints" ? "Prints" : "Time");
    b.type = "button";
    b.addEventListener("click", () => { _statsMetric = k; renderStats(); });
    seg.append(b);
  });
  const view = _el("div", "seg seg-sm");
  [["chart", "Chart"], ["table", "Table"]].forEach(([k, t]) => {
    const b = _el("button", k === _statsView ? "active" : "", t);
    b.type = "button";
    b.addEventListener("click", () => { _statsView = k; renderStats(); });
    view.append(b);
  });
  bar.append(seg, view);
  card.append(bar);
  if (_statsView === "table") {
    const tw = _el("div", "stats-table-wrap");
    tw.append(_statsTable(d));
    card.append(tw);
  } else {
    const wrap = _el("div", "stats-chart-wrap");
    const tip = _el("div", "stats-tip");
    tip.hidden = true;
    wrap.append(_renderStatsChart(d, tip), tip);
    card.append(wrap);
  }
  frag.append(card);

  const mats = Object.entries(d.by_material).map(([k, v]) => ({ label: k, value: v, text: `${_fmtNum(v, 0)} g` }));
  frag.append(_barList("Filament by material", mats));
  const prs = Object.entries(d.by_printer).map(([k, v]) => ({
    label: k, value: v.grams, text: `${_fmtNum(v.grams, 0)} g`, sub: `${v.prints} prints · ${_fmtNum(v.hours, 1)} h` }));
  frag.append(_barList("By printer", prs));

  const res = _el("div", "stats-card");
  res.append(_el("h4", null, "Results"));
  [["complete", "Finished", "✓", "ok"], ["cancelled", "Stopped", "–", "muted"], ["error", "Failed", "!", "bad"]].forEach(([k, label, icon, cls]) => {
    const n = d.results[k], row = _el("div", "result-row");
    row.append(_el("span", `result-icon ${cls}`, icon), _el("span", "result-label", label),
               _el("strong", null, `${n}  (${_fmtNum(100 * n / d.prints, 0)}%)`));
    res.append(row);
  });
  frag.append(res);

  const why = Object.entries(d.failure_reasons);
  if (why.length) {
    const wc = _el("div", "stats-card");
    wc.append(_el("h4", null, "Why prints stopped"));
    why.slice(0, 8).forEach(([reason, n]) => {
      const row = _el("div", "result-row");
      row.append(_el("span", "result-label", _REASON_CATEGORY_LABEL[reason] || reason), _el("strong", null, String(n)));
      wc.append(row);
    });
    frag.append(wc);
  }
  body.replaceChildren(frag);
}

function _fillStatsPrinters() {
  const sel = document.getElementById("stats-printer");
  const cur = sel.value;
  sel.replaceChildren(new Option("All printers", ""));
  Object.values(printers).forEach(p => sel.add(new Option(p.name || p.id, p.id)));
  sel.value = [...sel.options].some(o => o.value === cur) ? cur : "";
}

function _showStatsTab(tab) {
  _statsTab = tab;
  document.querySelectorAll("#stats-tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  const detail = tab === "prints" && _openPrintId;
  document.getElementById("stats-body").hidden   = tab !== "overview";
  document.getElementById("prints-body").hidden  = tab !== "prints" || !!detail;
  document.getElementById("print-detail").hidden = !detail;
  document.getElementById("stats-search").hidden = tab !== "prints";
  document.getElementById("stats-filterbar").hidden = !!detail;
  document.getElementById("stats-tabs").hidden = !!detail;
  if (tab === "overview") loadStats(); else renderPrints();
}

const openStats = () => {
  statsPanel.classList.add("open");
  historyBackdrop.classList.add("open");
  btnStats.classList.add("active");
  _fillStatsPrinters();
  _showStatsTab(_statsTab);
};
const closeStats = () => {
  statsPanel.classList.remove("open");
  historyBackdrop.classList.remove("open");
  btnStats.classList.remove("active");
};
const _refreshStats = () => { if (_statsTab === "overview") loadStats(); else renderPrints(); };
btnStats.addEventListener("click", openStats);
document.getElementById("btn-stats-close").addEventListener("click", closeStats);
document.getElementById("stats-printer").addEventListener("change", _refreshStats);
document.getElementById("stats-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) { _openPrintId = null; _showStatsTab(b.dataset.tab); }
});
document.getElementById("stats-search").addEventListener("input", () => renderPrints());
document.getElementById("stats-range").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-range]");
  if (!b) return;
  _statsRange = b.dataset.range;
  document.querySelectorAll("#stats-range button").forEach(x => x.classList.toggle("active", x === b));
  document.getElementById("stats-custom").hidden = _statsRange !== "custom";
  if (_statsRange === "custom") {
    const { from, to } = { from: document.getElementById("stats-from"), to: document.getElementById("stats-to") };
    if (!to.value) to.value = _isoDay(new Date());
    if (!from.value) { const d = new Date(); d.setDate(d.getDate() - 29); from.value = _isoDay(d); }
  }
  _refreshStats();
});
["stats-from", "stats-to"].forEach(id => document.getElementById(id).addEventListener("change", _refreshStats));

// ─── Prints list and per-print detail (the old History, now inside Stats) ────
function _endStateOf(e) {
  if (e.end_state === "complete" || e.end_state === "cancelled" || e.end_state === "error") return e.end_state;
  return e.completed ? "complete" : "cancelled";
}
const _RESULT_UI = {
  complete:  { label: "Finished", icon: "✓", cls: "ok" },
  cancelled: { label: "Stopped",  icon: "–", cls: "muted" },
  error:     { label: "Failed",   icon: "!", cls: "bad" },
};

function _visiblePrints() {
  const { from, to } = _statsPeriod();
  const pr = document.getElementById("stats-printer").value;
  const q = (document.getElementById("stats-search").value || "").trim().toLowerCase();
  return history.filter(e => {
    const day = (e.timestamp || "").slice(0, 10);
    if (from && day < from) return false;
    if (to && day > to) return false;
    if (pr && e.printer_id !== pr) return false;
    if (q && !`${e.filename || ""} ${e.reference || ""}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

function _printThumb(e) {
  if (e.snapshot && e.id && featureEnabled("print_snapshot")) {
    const img = _el("img", "print-thumb");
    img.src = `/api/snapshot/${e.id}`;
    img.alt = "Picture of the print";
    img.loading = "lazy";
    return img;
  }
  return _el("div", "print-thumb print-thumb-empty", "—");
}

function renderPrints() {
  if (_statsTab !== "prints") return;
  if (_openPrintId) { _renderPrintDetail(); return; }
  const host = document.getElementById("prints-body");
  const rows = _visiblePrints();
  const frag = document.createDocumentFragment();
  const g = rows.reduce((s, e) => s + (e.filament_g || 0), 0);
  frag.append(_el("div", "prints-count", rows.length
    ? `${rows.length} print${rows.length === 1 ? "" : "s"} · ${_fmtNum(g, 0)} g`
    : (history.length ? "No prints match." : "No prints logged yet.")));
  rows.forEach(e => {
    const st = _RESULT_UI[_endStateOf(e)];
    const row = _el("button", "print-row");
    row.type = "button";
    row.addEventListener("click", () => { _openPrintId = e.id; _showStatsTab("prints"); });
    const main = _el("div", "print-row-main");
    const title = _el("div", "print-row-title");
    title.append(_el("span", "print-row-name", e.filename || "Unnamed print"));
    if (e.reference) title.append(_el("span", "ref-badge", "#" + e.reference));
    const meta = _el("div", "print-row-meta",
      `${(e.timestamp || "").replace("T", " ").slice(0, 16)} · ${e.printer_name || "Printer"} · ${formatTime(e.print_time_s || 0)} · ${_fmtNum(e.filament_g || 0, 1)} g`);
    main.append(title, meta);
    const icon = _el("span", `result-icon ${st.cls}`, st.icon);
    icon.title = st.label;
    icon.setAttribute("aria-label", st.label);
    row.append(_printThumb(e), main, icon);
    frag.append(row);
  });
  host.replaceChildren(frag);
}

function _fact(label, value) {
  const f = _el("div", "fact");
  f.append(_el("dt", null, label), _el("dd", null, value));
  return f;
}
function _fmtWhen(ts) { return ts ? ts.replace("T", " ").slice(0, 16) : "—"; }

function _renderPrintDetail() {
  const host = document.getElementById("print-detail");
  const e = history.find(x => x.id === _openPrintId);
  if (!e) { _openPrintId = null; _showStatsTab("prints"); return; }
  const st = _RESULT_UI[_endStateOf(e)];
  const frag = document.createDocumentFragment();

  const back = _el("button", "btn btn-secondary btn-sm", "← Back to prints");
  back.type = "button";
  back.addEventListener("click", () => { _openPrintId = null; _showStatsTab("prints"); });
  frag.append(back);

  const head = _el("div", "print-detail-head");
  head.append(_el("h4", null, e.filename || "Unnamed print"));
  const badge = _el("span", `result-pill ${st.cls}`, `${st.icon} ${st.label}`);
  head.append(badge);
  frag.append(head);

  if (e.snapshot && e.id && featureEnabled("print_snapshot")) {
    const img = _el("img", "print-photo");
    img.src = `/api/snapshot/${e.id}`;
    img.alt = "Picture taken when the print ended";
    img.addEventListener("click", () => openSnapshot(e.id));
    frag.append(img);
  } else {
    frag.append(_el("div", "print-photo print-photo-empty", "No picture was saved for this print"));
  }

  // Reference number
  const ref = _el("div", "ref-edit");
  const lab = _el("label", null, "Reference number");
  const input = _el("input");
  input.type = "text"; input.maxLength = 40; input.value = e.reference || ""; input.placeholder = "e.g. 042";
  input.id = "print-ref-input";
  lab.append(input);
  const save = _el("button", "btn btn-primary btn-sm", "Save");
  save.type = "button";
  const doSave = async () => {
    save.disabled = true;
    try {
      const r = await fetch(`/api/history/${encodeURIComponent(e.id)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reference: input.value }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { toast(d.error || "Could not save the reference", true); return; }
      e.reference = d.reference;
      input.value = d.reference;
      toast(d.reference ? `Reference saved: ${d.reference}` : "Reference cleared");
    } catch (err) {
      toast("Could not save the reference: " + err.message, true);
    } finally { save.disabled = false; }
  };
  save.addEventListener("click", doSave);
  input.addEventListener("keydown", ev => { if (ev.key === "Enter") doSave(); });
  ref.append(lab, save);
  frag.append(ref);

  // Facts
  const facts = _el("dl", "facts");
  facts.append(
    _fact("Printer", e.printer_name || "—"),
    _fact("Started", _fmtWhen(e.started_at)),
    _fact("Ended", _fmtWhen(e.timestamp)),
    _fact("Print time", formatTime(e.print_time_s || 0)),
    _fact("Filament", `${_fmtNum((e.filament_mm || 0) / 1000, 2)} m · ${_fmtNum(e.filament_g || 0, 1)} g`),
  );
  if (e.material || e.vendor) facts.append(_fact("Material", [e.vendor, e.material].filter(Boolean).join(" ")));
  if (Array.isArray(e.spools) && e.spools.length) {
    const names = e.spools.map(sp => {
      const full = (typeof spools !== "undefined" ? spools : []).find(x => x.id === sp.id);
      return `${full ? spoolName(full) : "Spool #" + sp.id}: ${_fmtNum(sp.g, 1)} g`;
    });
    facts.append(_fact(e.spools.length > 1 ? "Spools used" : "Spool", names.join("\n")));
  }
  if (_endStateOf(e) !== "complete") {
    const cause = e.error_message || _REASON_CATEGORY_LABEL[e.stop_reason] || e.stop_reason || "Cause not known";
    const by = e.initiated_by ? ` (${_REASON_INITIATOR_LABEL[e.initiated_by] || e.initiated_by})` : "";
    facts.append(_fact("Cause", cause + (e.error_code ? ` · code ${e.error_code}` : "") + by));
  }
  frag.append(facts);

  if (Array.isArray(e.pauses) && e.pauses.length) {
    const pc = _el("div", "stats-card");
    pc.append(_el("h4", null, `Pauses (${e.pauses.length})`));
    e.pauses.forEach(p => {
      const row = _el("div", "result-row");
      const dur = p.duration_s != null ? ` · ${formatTime(p.duration_s)}` : "";
      const who = _REASON_INITIATOR_LABEL[p.initiated_by] || p.initiated_by || "";
      row.append(_el("span", "result-label", `${_fmtWhen(p.since).slice(11)}${dur} — ${_REASON_CATEGORY_LABEL[p.category] || p.category || "paused"}${who ? " (" + who + ")" : ""}`));
      pc.append(row);
    });
    frag.append(pc);
  }
  host.replaceChildren(frag);
}

