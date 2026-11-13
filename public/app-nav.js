// Pages: the side menu switches what the main area shows (Printers, Stats,
// Spools, Service, Queue, Settings) instead of sliding panels over it. Small
// dialogs (add spool, add printer, file browser ...) still open on top.

"use strict";

const _PAGES = {
  printers: { btn: "btn-home" },
  stats:    { btn: "btn-stats",        open: () => openStats(),    close: () => closeStats() },
  spools:   { btn: "btn-spools",       open: () => openSpools(),   close: () => closeSpools() },
  service:  { btn: "btn-service",      open: () => openService(),  close: () => closeService() },
  queue:    { btn: "btn-queue",        open: () => openQueue(),    close: () => closeQueue() },
  me:       { btn: "signed-in-as",     open: () => openMe(),       close: () => closeMe() },
  settings: { btn: "btn-app-settings", open: () => _openSettings(),
              close: () => document.getElementById("modal-app-settings")?.classList.remove("open") },
};
let _currentPage = "printers";

function showPage(name, push = true) {
  if (!_PAGES[name]) name = "printers";
  if (name === _currentPage && push) {
    if (name === "settings") _backToSettingsMenu();     // tapping Settings again goes to its menu
    window.scrollTo(0, 0);
    return;
  }
  for (const [k, p] of Object.entries(_PAGES)) if (k !== name && p.close) p.close();
  _currentPage = name;
  document.body.dataset.page = name;
  for (const [k, p] of Object.entries(_PAGES)) document.getElementById(p.btn)?.classList.toggle("active", k === name);
  _PAGES[name].open?.();
  window.scrollTo(0, 0);
  if (push) window.history.pushState({ page: name }, "", name === "printers" ? location.pathname : "#" + name);
}

for (const [k, p] of Object.entries(_PAGES)) {
  document.getElementById(p.btn)?.addEventListener("click", () => showPage(k));
}
// The browser's (or phone's) back button walks back through the pages. ("history" on its own
// is the print history array in app-core.js, so window.history is spelled out.)
window.addEventListener("popstate", e => showPage(e.state?.page || (location.hash.slice(1) || "printers"), false));
// A reload keeps you on the page you were on (#stats, #spools ...).
const _startPage = location.hash.slice(1);
document.body.dataset.page = "printers";
window.history.replaceState({ page: "printers" }, "", location.pathname);
if (_PAGES[_startPage] && _startPage !== "printers") showPage(_startPage);
