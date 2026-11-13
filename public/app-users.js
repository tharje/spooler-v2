// Users and roles (RBAC) admin UI. The menu entry is only revealed to users
// with the "users.manage" permission (see app-boot.js). All writes go through
// /api/users and /api/roles, which the server also guards.
"use strict";

let _rolesCache = [];
let _permsCatalog = [];
let _printersCache = [];

async function _loadUsersAndRoles() {
  try {
    const [ur, rr, pr] = await Promise.all([
      fetch("/api/users"), fetch("/api/roles"), fetch("/api/printers"),
    ]);
    if (!ur.ok || !rr.ok) { toast("Could not load users", true); return; }
    const u = await ur.json();
    const r = await rr.json();
    _printersCache = pr.ok ? (await pr.json()) : [];
    _rolesCache = r.roles || [];
    _permsCatalog = r.permissions || [];
    _renderRoleOptions();
    _renderUsers(u.users || [], u.current || "");
    _renderRoles();
  } catch (_) {
    toast("Could not load users", true);
  }
}

// A reusable printer-visibility picker. `current` is "*" (all), an array of
// printer ids, or null (inherit the role — only offered when allowInherit).
// Returns { el, get } where get() yields "*", an array, or null.
function _printerPicker(current, allowInherit) {
  const wrap = _el("div", "printer-picker");
  const mode = document.createElement("select");
  if (allowInherit) mode.append(new Option("Inherit from role", "inherit"));
  mode.append(new Option("All printers", "all"), new Option("Choose printers", "custom"));
  const boxes = _el("div", "printer-choices");

  _printersCache.forEach(p => {
    const label = _el("label", "printer-choice");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = p.id;
    cb.checked = Array.isArray(current) && current.includes(p.id);
    label.append(cb, document.createTextNode(" " + (p.name || p.id)));
    boxes.append(label);
  });

  const initial = allowInherit && (current === null || current === undefined)
    ? "inherit" : (current === "*" ? "all" : "custom");
  mode.value = initial;
  const sync = () => { boxes.style.display = mode.value === "custom" ? "" : "none"; };
  mode.addEventListener("change", sync);
  sync();

  wrap.append(mode, boxes);
  return {
    el: wrap,
    get() {
      if (mode.value === "inherit") return null;
      if (mode.value === "all") return "*";
      return [...boxes.querySelectorAll("input:checked")].map(c => c.value);
    },
  };
}

function _renderRoleOptions() {
  const sel = document.getElementById("new-user-role");
  if (!sel) return;
  sel.replaceChildren(..._rolesCache.map(r => {
    const o = document.createElement("option");
    o.value = r.name;
    o.textContent = r.label || r.name;
    return o;
  }));
}

function _renderUsers(usersArr, current) {
  const host = document.getElementById("users-list");
  if (!host) return;
  if (!usersArr.length) { host.replaceChildren(_el("p", "report-desc", "No users yet.")); return; }
  host.replaceChildren(...usersArr.map(u => {
    const wrap = _el("div", "user-block");
    const row = _el("div", "user-row");
    row.append(_el("span", "user-name", u.username + (u.username === current ? " (you)" : "")));

    const sel = document.createElement("select");
    sel.replaceChildren(..._rolesCache.map(r => {
      const o = document.createElement("option");
      o.value = r.name;
      o.textContent = r.label || r.name;
      if (r.name === u.role) o.selected = true;
      return o;
    }));
    sel.addEventListener("change", () => _updateUser(u.username, { role: sel.value }));
    row.append(sel);

    const pw = _el("button", "btn btn-secondary btn-sm", "Reset password");
    pw.addEventListener("click", () => {
      const p = prompt(`New password for ${u.username} (at least 6 characters):`);
      if (p) _updateUser(u.username, { password: p });
    });
    row.append(pw);

    const del = _el("button", "btn btn-secondary btn-sm", "Delete");
    del.addEventListener("click", () => {
      if (confirm(`Delete user "${u.username}"? This cannot be undone.`)) _deleteUser(u.username);
    });
    row.append(del);
    wrap.append(row);

    // Per-user printer override (null = inherit the role's list).
    const pr = _el("div", "user-printers");
    pr.append(_el("span", "user-printers-label", "Printers:"));
    const picker = _printerPicker(u.printers === undefined ? null : u.printers, true);
    pr.append(picker.el);
    const savePr = _el("button", "btn btn-secondary btn-sm", "Save");
    savePr.addEventListener("click", () => _updateUser(u.username, { printers: picker.get() }));
    pr.append(savePr);
    wrap.append(pr);
    return wrap;
  }));
}

async function _updateUser(username, body) {
  const r = await fetch(`/api/users/${encodeURIComponent(username)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok) { toast("Saved"); _loadUsersAndRoles(); }
  else { toast(d.error || "Could not save", true); _loadUsersAndRoles(); }
}

async function _deleteUser(username) {
  const r = await fetch(`/api/users/${encodeURIComponent(username)}`, { method: "DELETE" });
  const d = await r.json().catch(() => ({}));
  if (r.ok) { toast("User deleted"); _loadUsersAndRoles(); }
  else toast(d.error || "Could not delete", true);
}

document.getElementById("btn-add-user")?.addEventListener("click", async () => {
  const nameEl = document.getElementById("new-user-name");
  const passEl = document.getElementById("new-user-pass");
  const name = nameEl.value.trim();
  const pass = passEl.value;
  const role = document.getElementById("new-user-role").value;
  if (!name || !pass) { toast("Username and password are required", true); return; }
  const r = await fetch("/api/users", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: name, password: pass, role }),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok) {
    toast(`User "${name}" added`);
    nameEl.value = ""; passEl.value = "";
    _loadUsersAndRoles();
  } else toast(d.error || "Could not add user", true);
});

function _renderRoles() {
  const host = document.getElementById("roles-list");
  if (!host) return;
  host.replaceChildren(..._rolesCache.map(role => {
    const box = _el("div", "role-box");

    const head = _el("div", "role-head");
    head.append(_el("strong", null, role.label || role.name));
    if (role.protected) head.append(_el("span", "role-badge", "full access"));
    else if (role.builtin) head.append(_el("span", "role-badge", "built-in"));
    if (role.in_use) head.append(_el("span", "role-note", `${role.in_use} user(s)`));
    box.append(head);

    const perms = _el("div", "role-perms");
    _permsCatalog.forEach(p => {
      const label = _el("label", "role-perm");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = p.key;
      cb.checked = role.all || role.permissions.includes(p.key);
      cb.disabled = !!role.protected;                 // Admin always keeps everything
      label.append(cb, document.createTextNode(" " + p.label));
      perms.append(label);
    });
    box.append(perms);

    let picker = null;
    if (!role.protected) {
      box.append(_el("div", "role-sub", "Printers this role can see"));
      picker = _printerPicker(role.all_printers ? "*" : role.printers, false);
      box.append(picker.el);
    }

    const actions = _el("div", "role-actions");
    if (!role.protected) {
      const save = _el("button", "btn btn-primary btn-sm", "Save role");
      save.addEventListener("click", () => {
        const picked = [...perms.querySelectorAll("input:checked")].map(c => c.value);
        _saveRole(role.name, { permissions: picked, printers: picker.get() });
      });
      actions.append(save);
    }
    if (!role.builtin && !role.protected) {
      const del = _el("button", "btn btn-secondary btn-sm", "Delete role");
      del.addEventListener("click", () => {
        if (confirm(`Delete role "${role.name}"?`)) _deleteRole(role.name);
      });
      actions.append(del);
    }
    box.append(actions);
    return box;
  }));
}

async function _saveRole(name, body) {
  const r = await fetch(`/api/roles/${encodeURIComponent(name)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok) { toast("Role saved"); _loadUsersAndRoles(); }
  else toast(d.error || "Could not save role", true);
}

async function _deleteRole(name) {
  const r = await fetch(`/api/roles/${encodeURIComponent(name)}`, { method: "DELETE" });
  const d = await r.json().catch(() => ({}));
  if (r.ok) { toast("Role deleted"); _loadUsersAndRoles(); }
  else toast(d.error || "Could not delete role", true);
}

document.getElementById("btn-add-role")?.addEventListener("click", async () => {
  const nameEl = document.getElementById("new-role-name");
  const labelEl = document.getElementById("new-role-label");
  const name = nameEl.value.trim();
  if (!name) { toast("Give the role a name", true); return; }
  const r = await fetch("/api/roles", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, label: labelEl.value.trim(), permissions: ["view"] }),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok) {
    toast("Role created — now tick its permissions below and Save");
    nameEl.value = ""; labelEl.value = "";
    _loadUsersAndRoles();
  } else toast(d.error || "Could not create role", true);
});

// Navigation (these helpers live in app-settings.js, loaded earlier).
document.getElementById("btn-settings-goto-users")?.addEventListener("click", () => {
  _showSettingsPage(_settingsUsersPage);
  _loadUsersAndRoles();
});
document.getElementById("btn-settings-back-users")?.addEventListener("click", _backToSettingsMenu);
