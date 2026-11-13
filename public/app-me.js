// "Me" page: the signed-in user's own profile, prints, milestones, devices,
// queue jobs, commands and account. Opened from the user name in the side menu.

"use strict";

const mePanel = document.getElementById("panel-me");
let _meData = null;

const openMe = () => {
  mePanel.classList.add("open");
  loadMe();
};
const closeMe = () => mePanel.classList.remove("open");

async function loadMe() {
  const body = document.getElementById("me-body");
  if (!_meData) body.textContent = "Loading…";
  try {
    const r = await fetch("/api/me");
    if (!r.ok) throw new Error("HTTP " + r.status);
    _meData = await r.json();
  } catch (e) {
    body.textContent = "Could not load your page: " + e.message;
    return;
  }
  renderMe();
}

function _meEl(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function _meCard(title, extraCls = "") {
  const c = _meEl("section", "me-card " + extraCls);
  if (title) c.append(_meEl("h4", null, title));
  return c;
}

function _meHours(secs) {
  const h = Math.floor(secs / 3600), m = Math.round((secs % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

function _meGrams(g) {
  return g >= 1000 ? `${(g / 1000).toFixed(2)} kg` : `${Math.round(g)} g`;
}

function _meTile(label, value, sub) {
  const t = _meEl("div", "stat-tile");
  t.append(_meEl("div", "stat-tile-label", label), _meEl("div", "stat-tile-value", value));
  if (sub) t.append(_meEl("div", "stat-tile-sub", sub));
  return t;
}

function _meDate(ts) {
  if (!ts) return "";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  return isNaN(d) ? "" : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

// sha-256 of this browser's push endpoint, to mark "this device" in the list.
// crypto.subtle only exists on https/localhost; without it nothing is marked.
async function _meThisDeviceId() {
  try {
    if (!crypto?.subtle || !navigator.serviceWorker) return null;
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = await reg?.pushManager?.getSubscription();
    if (!sub) return null;
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(sub.endpoint));
    return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
  } catch (_) { return null; }
}

async function renderMe() {
  const d = _meData, body = document.getElementById("me-body");
  if (!d) return;
  const frag = document.createDocumentFragment();
  const s = d.stats;

  // Who I am
  const head = _meCard(null, "me-head");
  const av = _meEl("div", "me-avatar", (d.user || "?").slice(0, 1).toUpperCase());
  const who = _meEl("div", "me-who");
  who.append(_meEl("strong", null, d.user || "Not signed in"));
  if (d.role.label) who.append(_meEl("span", "me-role", d.role.label));
  const since = s.first_at ? `First print from Spooler ${_meDate(s.first_at)}` : "";
  if (since) who.append(_meEl("span", "me-sub", since));
  head.append(av, who);
  frag.append(head);

  // My prints
  const prints = _meCard("My prints");
  if (!s.prints) {
    prints.append(_meEl("p", "me-note", "Nothing yet. Prints you start from Spooler (file browser, queue or upload) are counted here from now on."));
  } else {
    const tiles = _meEl("div", "stat-tiles");
    tiles.append(
      _meTile("Prints", String(s.prints), `${s.complete} finished`),
      _meTile("Success rate", s.success_rate == null ? "—" : s.success_rate + "%", "finished without being stopped"),
      _meTile("Filament", _meGrams(s.grams), s.favourite_material ? `mostly ${s.favourite_material}` : ""),
      _meTile("Print time", `${s.hours} h`, ""),
    );
    prints.append(tiles);
    const rec = _meEl("div", "me-records");
    const recRow = (icon, label, value, sub) => {
      const r = _meEl("div", "me-record");
      r.append(_meEl("span", "me-record-icon", icon));
      const t = _meEl("div", "me-record-text");
      t.append(_meEl("span", null, label), _meEl("strong", null, value));
      if (sub) t.append(_meEl("small", null, sub));
      r.append(t);
      return r;
    };
    if (s.longest_s) rec.append(recRow("⏱", "Longest print", _meHours(s.longest_s), s.longest_file));
    if (s.biggest_g) rec.append(recRow("🧵", "Most filament in one print", _meGrams(s.biggest_g), s.biggest_file));
    if (s.favourite_printer) rec.append(recRow("🤝", "Printer buddy", s.favourite_printer,
                                               `${s.favourite_printer_prints} of your prints`));
    if (rec.children.length) prints.append(rec);
  }
  frag.append(prints);

  // Milestones
  const ms = _meCard("Milestones");
  const grid = _meEl("div", "me-milestones");
  const reached = s.milestones.filter(m => m.reached).length;
  s.milestones.forEach(m => {
    const b = _meEl("div", "me-badge" + (m.reached ? " reached" : ""));
    b.append(_meEl("span", "me-badge-icon", m.reached ? "★" : "☆"), _meEl("span", "me-badge-title", m.title));
    if (!m.reached) {
      const bar = _meEl("div", "me-badge-bar");
      const fill = _meEl("div");
      fill.style.width = Math.round(m.progress * 100) + "%";
      bar.append(fill);
      b.append(bar);
    }
    grid.append(b);
  });
  ms.querySelector("h4").textContent = `Milestones · ${reached} of ${s.milestones.length}`;
  ms.append(grid);
  frag.append(ms);

  // What I can do
  const can = _meCard("What you can do");
  can.append(_meEl("p", "me-note", d.all_printers ? "You see all printers." :
    d.printers.length ? `You see: ${d.printers.map(p => p.name).join(", ")}` : "You don't see any printers yet."));
  // The full list is long, so it folds away.
  const det = _meEl("details", "me-perms-box");
  det.append(_meEl("summary", null, `${d.permissions.length} permissions from your role`));
  const ul = _meEl("ul", "me-perms");
  d.permissions.forEach(p => ul.append(_meEl("li", null, p.label)));
  det.append(ul);
  can.append(det);
  frag.append(can);

  // Notifications and devices
  const nt = _meCard("Notifications");
  const n = d.notifications;
  nt.append(_meEl("p", "me-note",
    `${n.events_on} event types on (${n.own_events ? "your own choice" : "following the shared settings"})` +
    (n.channels_on ? ` · ${n.channels_on} own channel${n.channels_on > 1 ? "s" : ""}` : "")));
  const thisId = await _meThisDeviceId();
  if (d.devices.length) {
    const list = _meEl("div", "me-list");
    d.devices.forEach(dev => {
      const row = _meEl("div", "me-row");
      const t = _meEl("div", "me-row-text");
      t.append(_meEl("strong", null, dev.name + (dev.id === thisId ? " · this device" : "")));
      if (dev.added) t.append(_meEl("small", null, `Added ${_meDate(dev.added)}`));
      const rm = _meEl("button", "btn btn-sm btn-secondary", "Remove");
      rm.addEventListener("click", () => _meRemoveDevice(dev));
      row.append(t, rm);
      list.append(row);
    });
    nt.append(list);
  } else {
    nt.append(_meEl("p", "me-note", "No devices get push notifications for you yet."));
  }
  const go = _meEl("button", "btn btn-sm btn-secondary", "Notification settings");
  go.addEventListener("click", () => { showPage("settings"); document.getElementById("btn-settings-goto-notifications")?.click(); });
  nt.append(go);
  frag.append(nt);

  // My queue and my commands
  if (d.jobs.length) {
    const q = _meCard("My jobs in the queue");
    const list = _meEl("div", "me-list");
    d.jobs.forEach(j => {
      const row = _meEl("div", "me-row");
      const t = _meEl("div", "me-row-text");
      t.append(_meEl("strong", null, j.filename || "?"), _meEl("small", null, [j.status, j.printer].filter(Boolean).join(" · ")));
      row.append(t);
      list.append(row);
    });
    q.append(list);
    frag.append(q);
  }
  if (d.commands.length) {
    const c = _meCard("My recent commands");
    const list = _meEl("div", "me-list");
    d.commands.slice(0, 8).forEach(e => {
      const row = _meEl("div", "me-row");
      const t = _meEl("div", "me-row-text");
      const label = (typeof _CTL_ACTION_LABEL !== "undefined" && _CTL_ACTION_LABEL[e.action]) || e.action;
      const detail = typeof _ctlParamText === "function" ? _ctlParamText(e) : "";
      t.append(_meEl("strong", null, `${label}${detail ? " · " + detail : ""}`),
               _meEl("small", null, `${e.printer} · ${new Date(e.time * 1000).toLocaleString()}`));
      row.append(t, _meEl("span", e.result === "ok" ? "me-ok" : "me-fail", e.result === "ok" ? "OK" : e.result));
      list.append(row);
    });
    c.append(list);
    frag.append(c);
  }

  // Account
  const acc = _meCard("Account");
  const rowBtn = (label, sub, btnText, onClick, cls = "btn-secondary") => {
    const row = _meEl("div", "me-row");
    const t = _meEl("div", "me-row-text");
    t.append(_meEl("strong", null, label));
    if (sub) t.append(_meEl("small", null, sub));
    const b = _meEl("button", "btn btn-sm " + cls, btnText);
    b.addEventListener("click", onClick);
    row.append(t, b);
    return row;
  };
  const list = _meEl("div", "me-list");
  list.append(rowBtn("Password", "", "Change", () => { showPage("settings"); document.getElementById("btn-settings-goto-password")?.click(); }));
  const light = document.body.classList.contains("light-mode");
  list.append(rowBtn("Appearance", light ? "Light mode" : "Dark mode", light ? "Use dark" : "Use light", () => {
    const tog = document.getElementById("toggle-light-mode");
    if (tog) { tog.checked = !tog.checked; tog.dispatchEvent(new Event("change")); }
    renderMe();
  }));
  if (d.sessions > 1) {
    list.append(rowBtn("Signed in", `on ${d.sessions} devices or browsers`, "Sign out the others", _meSignOutOthers));
  } else if (d.user) {
    list.append(rowBtn("Signed in", "only here", "Sign out", signOut));
  }
  acc.append(list);
  frag.append(acc);

  body.replaceChildren(frag);
}

async function _meRemoveDevice(dev) {
  if (!confirm(`Stop push notifications to ${dev.name}?`)) return;
  const r = await fetch("/api/me/devices/" + encodeURIComponent(dev.id), { method: "DELETE" });
  if (!r.ok) { toast("Could not remove the device", true); return; }
  toast("Device removed");
  loadMe();
}

async function _meSignOutOthers() {
  if (!confirm("Sign out on all your other devices and browsers? You stay signed in here.")) return;
  const r = await fetch("/api/me/sign-out-others", { method: "POST" });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || "Could not sign out the others", true); return; }
  toast(`Signed out on ${d.signed_out} other device${d.signed_out === 1 ? "" : "s"}`);
  loadMe();
}
