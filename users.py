"""
Users, roles and permissions (RBAC).

Spooler started as a single login (one username + password in auth.json). This
module turns that into multiple named users, each assigned a role, where a role
is just a set of permissions. An admin creates users and freely defines roles
and what each role may do.

Design notes
------------
- Stored in DATA_DIR/users.json (0600), same pattern as api_tokens.py.
- Passwords are bcrypt hashes, never stored in clear.
- The built-in "admin" role always has every permission, including future ones,
  and cannot be deleted or stripped of user-management — this is what prevents
  locking yourself out. The other built-in roles (operator, viewer) are only a
  starting point and may be edited or deleted.
- At least one user with the admin role must always exist.
- On first load we migrate the legacy single user (auth.json, or the
  SPOOLER_USERNAME / SPOOLER_PW_HASH env vars) in as an admin.

Jobs carry an owner (the user who queued them). "job.cancel_own" lets a user
cancel their own jobs; "job.cancel_any" lets a higher role cancel anyone's.
"""

import json
import os
import threading
import time
from pathlib import Path

try:
    import bcrypt as _bcrypt
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False

import persistence
from persistence import _atomic_write

# ── Permission catalog ──────────────────────────────────────────────────────
# (key, human label) — the label is shown in the role editor. Add new
# permissions here; the admin role picks them up automatically via ALL.
PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("view",            "See dashboard, printers, cameras, status and history"),
    ("print.queue",     "Upload files and add prints to the queue"),
    ("print.start",     "Start and confirm prints"),
    ("job.cancel_own",  "Cancel or remove own jobs"),
    ("job.cancel_any",  "Cancel or remove anyone's jobs"),
    ("printer.control", "Control the printer (pause/resume/stop, light, print speed)"),
    ("printer.advanced", "Advanced control: temperatures, fans, axes, homing and bed levelling"),
    ("filament.manage", "Manage filament / Spoolman ledger"),
    ("settings.manage", "Change settings, features, integrations and notifications"),
    ("printers.manage", "Add, rename and remove printers and change their address"),
    ("notify.channels", "Set up own notification channels (Telegram, ntfy, Discord, webhook)"),
    ("maintenance",     "Run service tasks and diagnostics"),
    ("backup.manage",   "Create and restore backups"),
    ("apitokens.manage","Manage external API tokens"),
    ("users.manage",    "Manage users and roles"),
)
PERMISSION_KEYS: tuple[str, ...] = tuple(k for k, _ in PERMISSIONS)

# "*" in a role's permission list means "every permission, including future ones".
ALL = "*"

# Printer visibility. A role (and optionally a user) has a "printers" value that is
# either ALL_PRINTERS ("*" = every printer, including ones added later) or a list of
# printer ids. A user's value of None means "inherit the role's list". Not seeing a
# printer also means not being able to print to or control it.
ALL_PRINTERS = "*"

ADMIN_ROLE = "admin"

# Built-in roles shipped as a starting point. admin is protected; the rest are
# editable/deletable by the admin.
DEFAULT_ROLES: dict[str, dict] = {
    "admin":    {"permissions": [ALL], "printers": ALL_PRINTERS,
                 "builtin": True, "protected": True,
                 "label": "Admin — full access"},
    "operator": {"permissions": ["view", "print.queue", "print.start",
                                 "job.cancel_own", "printer.control"],
                 "printers": ALL_PRINTERS, "builtin": True, "protected": False,
                 "label": "Operator — can print and control the printer"},
    "viewer":   {"permissions": ["view"], "printers": ALL_PRINTERS,
                 "builtin": True, "protected": False,
                 "label": "Viewer — can only look"},
}

MAX_USERS = 50
MAX_USERNAME_LEN = 40
MAX_ROLE_LEN = 40

_lock = threading.RLock()


# ── Storage ─────────────────────────────────────────────────────────────────

def _file() -> Path:
    return persistence.DATA_DIR / "users.json"


def _blank() -> dict:
    return {"users": {}, "roles": {k: dict(v) for k, v in DEFAULT_ROLES.items()},
            "printers_manage_split": True}


def _load() -> dict:
    try:
        data = json.loads(_file().read_text())
        if not isinstance(data, dict) or "users" not in data or "roles" not in data:
            return _blank()
        return data
    except Exception:
        return _blank()


def _save(data: dict) -> None:
    _atomic_write(_file(), json.dumps(data, indent=2), mode=0o600)


# ── Migration from the legacy single-user model ─────────────────────────────

def ensure_initialised() -> None:
    """Create users.json on first run, migrating the old single login (auth.json
    or the SPOOLER_USERNAME / SPOOLER_PW_HASH env vars) in as the first admin."""
    with _lock:
        if _file().exists():
            # Make sure the built-in admin role is always present and sane.
            data = _load()
            changed = False
            if ADMIN_ROLE not in data["roles"]:
                data["roles"][ADMIN_ROLE] = dict(DEFAULT_ROLES[ADMIN_ROLE])
                changed = True
            # printers.manage was split out of settings.manage: roles that could
            # manage printers before keep that ability (once, then it's theirs to edit).
            if not data.get("printers_manage_split"):
                for role in data["roles"].values():
                    perms = role.get("permissions") or []
                    if "settings.manage" in perms and "printers.manage" not in perms:
                        perms.append("printers.manage")
                data["printers_manage_split"] = True
                changed = True
            if changed:
                _save(data)
            return

        data = _blank()
        legacy_user, legacy_hash = _read_legacy_credentials()
        if legacy_user and legacy_hash:
            data["users"][legacy_user] = {
                "pw_hash": legacy_hash,
                "role": ADMIN_ROLE,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        _save(data)


def _read_legacy_credentials() -> tuple[str, str]:
    """(username, pw_hash) from the old auth.json or env, or ('', '')."""
    env_user = os.getenv("SPOOLER_USERNAME", "")
    env_hash = os.getenv("SPOOLER_PW_HASH", "")
    if env_user and env_hash:
        return env_user, env_hash
    try:
        legacy = json.loads((persistence.DATA_DIR / "auth.json").read_text())
        return legacy.get("username", ""), legacy.get("pw_hash", "")
    except Exception:
        return "", ""


# ── Permissions ─────────────────────────────────────────────────────────────

def _role_permissions(data: dict, role: str) -> set[str]:
    r = data["roles"].get(role)
    if not r:
        return set()
    perms = r.get("permissions", [])
    if ALL in perms:
        return set(PERMISSION_KEYS)
    return {p for p in perms if p in PERMISSION_KEYS}


def permissions_for(username: str) -> set[str]:
    """The set of permission keys a user has (via their role)."""
    with _lock:
        data = _load()
        user = data["users"].get(username)
        if not user:
            return set()
        return _role_permissions(data, user.get("role", ""))


def has_permission(username: str, perm: str) -> bool:
    return perm in permissions_for(username)


# ── Printer visibility ──────────────────────────────────────────────────────

def _clean_printers(value):
    """Normalise a printers value to ALL_PRINTERS or a list of id strings. None
    (a user inheriting its role) is preserved by the callers, not here."""
    if value == ALL_PRINTERS or value is None:
        return ALL_PRINTERS
    if isinstance(value, (list, tuple)):
        return [str(p) for p in value]
    raise ValueError("printers must be \"*\" or a list of printer ids.")


def _resolve_printers(data: dict, username: str):
    """A user's effective printers value: their own override, or the role's."""
    user = data["users"].get(username)
    if not user:
        return []
    own = user.get("printers")
    if own is not None:
        return own
    role = data["roles"].get(user.get("role"), {})
    return role.get("printers", ALL_PRINTERS)


def visible_printer_ids(username: str, all_ids) -> set[str]:
    """Which of all_ids this user may see (and therefore act on)."""
    all_ids = set(all_ids)
    with _lock:
        eff = _resolve_printers(_load(), username)
    if eff == ALL_PRINTERS:
        return all_ids
    return {p for p in eff if p in all_ids}


def can_see_printer(username: str, printer_id: str, all_ids=None) -> bool:
    with _lock:
        eff = _resolve_printers(_load(), username)
    if eff == ALL_PRINTERS:
        return True
    return printer_id in set(eff)


def set_user_printers(username: str, printers) -> None:
    """Set a per-user printer override. None restores inheriting the role."""
    with _lock:
        data = _load()
        if username not in data["users"]:
            raise ValueError("No such user.")
        data["users"][username]["printers"] = None if printers is None else _clean_printers(printers)
        _save(data)


# ── Authentication ──────────────────────────────────────────────────────────

def authenticate(username: str, password: str) -> bool:
    """True if the username/password match a stored user."""
    if not (BCRYPT_AVAILABLE and _bcrypt and username and password):
        return False
    with _lock:
        user = _load()["users"].get(username)
    if not user:
        return False
    try:
        return _bcrypt.checkpw(password.encode(), user["pw_hash"].encode())
    except Exception:
        return False


def hash_password(password: str) -> str:
    if not (BCRYPT_AVAILABLE and _bcrypt):
        raise RuntimeError("bcrypt is not available")
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def any_users() -> bool:
    with _lock:
        return bool(_load()["users"])


def get_user(username: str) -> dict | None:
    with _lock:
        user = _load()["users"].get(username)
        return _public_user(username, user) if user else None


def _public_user(username: str, user: dict) -> dict:
    return {"username": username, "role": user.get("role", ""),
            "printers": user.get("printers"),   # None = inherit role, "*" = all, or a list
            "created": user.get("created")}


def list_users() -> list[dict]:
    with _lock:
        data = _load()
        return [_public_user(u, rec) for u, rec in sorted(data["users"].items())]


# ── User management ─────────────────────────────────────────────────────────

def _valid_username(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > MAX_USERNAME_LEN:
        raise ValueError("Username must be 1–%d characters." % MAX_USERNAME_LEN)
    if not all(c.isalnum() or c in "._-" for c in name):
        raise ValueError("Username may use letters, digits and . _ - only.")
    return name


def create_user(username: str, password: str, role: str, printers=None) -> dict:
    username = _valid_username(username)
    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    with _lock:
        data = _load()
        if username in data["users"]:
            raise ValueError("A user with that name already exists.")
        if len(data["users"]) >= MAX_USERS:
            raise ValueError(f"At most {MAX_USERS} users.")
        if role not in data["roles"]:
            raise ValueError("Unknown role.")
        data["users"][username] = {
            "pw_hash": hash_password(password),
            "role": role,
            "printers": None if printers is None else _clean_printers(printers),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _save(data)
        return _public_user(username, data["users"][username])


def delete_user(username: str) -> None:
    with _lock:
        data = _load()
        if username not in data["users"]:
            raise ValueError("No such user.")
        if _is_last_admin(data, username):
            raise ValueError("Cannot delete the last admin.")
        del data["users"][username]
        _save(data)


def set_password(username: str, password: str) -> None:
    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    with _lock:
        data = _load()
        if username not in data["users"]:
            raise ValueError("No such user.")
        data["users"][username]["pw_hash"] = hash_password(password)
        _save(data)


def set_role(username: str, role: str) -> None:
    with _lock:
        data = _load()
        if username not in data["users"]:
            raise ValueError("No such user.")
        if role not in data["roles"]:
            raise ValueError("Unknown role.")
        if _is_last_admin(data, username) and role != ADMIN_ROLE:
            raise ValueError("Cannot remove the last admin's admin role.")
        data["users"][username]["role"] = role
        _save(data)


def _is_last_admin(data: dict, username: str) -> bool:
    u = data["users"].get(username)
    if not u or u.get("role") != ADMIN_ROLE:
        return False
    admins = [n for n, r in data["users"].items() if r.get("role") == ADMIN_ROLE]
    return admins == [username]


# ── Role management ─────────────────────────────────────────────────────────

def list_roles() -> list[dict]:
    with _lock:
        data = _load()
        out = []
        for name, r in sorted(data["roles"].items()):
            perms = sorted(_role_permissions(data, name))
            out.append({
                "name": name,
                "label": r.get("label", name),
                "permissions": perms,
                "all": ALL in r.get("permissions", []),
                "printers": r.get("printers", ALL_PRINTERS),
                "all_printers": r.get("printers", ALL_PRINTERS) == ALL_PRINTERS,
                "builtin": bool(r.get("builtin")),
                "protected": bool(r.get("protected")),
                "in_use": sum(1 for u in data["users"].values() if u.get("role") == name),
            })
        return out


def _valid_role_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not name or len(name) > MAX_ROLE_LEN:
        raise ValueError("Role name must be 1–%d characters." % MAX_ROLE_LEN)
    if not all(c.isalnum() or c in "._-" for c in name):
        raise ValueError("Role name may use letters, digits and . _ - only.")
    return name


def _clean_permissions(perms) -> list[str]:
    if not isinstance(perms, list):
        raise ValueError("Permissions must be a list.")
    return [p for p in PERMISSION_KEYS if p in perms]


def create_role(name: str, permissions, label: str = "", printers=ALL_PRINTERS) -> dict:
    name = _valid_role_name(name)
    with _lock:
        data = _load()
        if name in data["roles"]:
            raise ValueError("A role with that name already exists.")
        data["roles"][name] = {
            "permissions": _clean_permissions(permissions),
            "printers": _clean_printers(printers),
            "builtin": False,
            "protected": False,
            "label": (label or name).strip()[:80],
        }
        _save(data)
    return {"name": name}


def update_role(name: str, permissions=None, label: str | None = None, printers=None) -> None:
    with _lock:
        data = _load()
        role = data["roles"].get(name)
        if not role:
            raise ValueError("No such role.")
        if role.get("protected"):
            # The admin role always keeps every permission and every printer; only
            # its label is free to change.
            if label is not None:
                role["label"] = label.strip()[:80]
            _save(data)
            return
        if permissions is not None:
            role["permissions"] = _clean_permissions(permissions)
        if printers is not None:
            role["printers"] = _clean_printers(printers)
        if label is not None:
            role["label"] = label.strip()[:80]
        _save(data)


def delete_role(name: str) -> None:
    with _lock:
        data = _load()
        role = data["roles"].get(name)
        if not role:
            raise ValueError("No such role.")
        if role.get("protected") or role.get("builtin"):
            raise ValueError("Built-in roles cannot be deleted.")
        in_use = [u for u, r in data["users"].items() if r.get("role") == name]
        if in_use:
            raise ValueError("Role is assigned to %d user(s); reassign them first." % len(in_use))
        del data["roles"][name]
        _save(data)


def can_cancel_job(username: str, job_owner: str | None) -> bool:
    """A user may cancel a job if they can cancel anyone's, or it is their own
    and they may cancel their own."""
    perms = permissions_for(username)
    if "job.cancel_any" in perms:
        return True
    return "job.cancel_own" in perms and job_owner is not None and job_owner == username
