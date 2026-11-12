"""Tests for the users/roles/permissions (RBAC) model."""

import json

import pytest

import persistence
import users


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    # conftest already points persistence.DATA_DIR at a tmp dir; make sure no
    # legacy env credentials leak in.
    monkeypatch.delenv("SPOOLER_USERNAME", raising=False)
    monkeypatch.delenv("SPOOLER_PW_HASH", raising=False)
    yield


def test_initialises_empty_with_builtin_roles():
    users.ensure_initialised()
    assert users.any_users() is False
    names = {r["name"] for r in users.list_roles()}
    assert {"admin", "operator", "viewer"} <= names


def test_migrates_legacy_auth_json_as_admin():
    (persistence.DATA_DIR / "auth.json").write_text(
        json.dumps({"username": "polly", "pw_hash": users.hash_password("secret1")})
    )
    users.ensure_initialised()
    assert users.any_users() is True
    u = users.get_user("polly")
    assert u and u["role"] == "admin"
    assert users.authenticate("polly", "secret1") is True
    assert users.authenticate("polly", "wrong") is False


def test_migrates_env_credentials(monkeypatch):
    monkeypatch.setenv("SPOOLER_USERNAME", "boss")
    monkeypatch.setenv("SPOOLER_PW_HASH", users.hash_password("hunter2x"))
    users.ensure_initialised()
    assert users.get_user("boss")["role"] == "admin"


def test_admin_role_has_every_permission():
    users.ensure_initialised()
    users.create_user("root", "secret1", "admin")
    perms = users.permissions_for("root")
    assert perms == set(users.PERMISSION_KEYS)
    assert users.has_permission("root", "users.manage")


def test_operator_and_viewer_permissions():
    users.ensure_initialised()
    users.create_user("kid", "secret1", "operator")
    users.create_user("grandma", "secret1", "viewer")
    kid = users.permissions_for("kid")
    assert "print.start" in kid and "job.cancel_own" in kid
    assert "users.manage" not in kid and "settings.manage" not in kid
    assert users.permissions_for("grandma") == {"view"}


def test_create_user_validation():
    users.ensure_initialised()
    with pytest.raises(ValueError):
        users.create_user("bad name!", "secret1", "viewer")
    with pytest.raises(ValueError):
        users.create_user("ok", "short", "viewer")       # password too short
    with pytest.raises(ValueError):
        users.create_user("ok", "secret1", "nope")        # unknown role
    users.create_user("ok", "secret1", "viewer")
    with pytest.raises(ValueError):
        users.create_user("ok", "secret1", "viewer")      # duplicate


def test_cannot_delete_or_demote_last_admin():
    users.ensure_initialised()
    users.create_user("admin1", "secret1", "admin")
    with pytest.raises(ValueError):
        users.delete_user("admin1")
    with pytest.raises(ValueError):
        users.set_role("admin1", "viewer")
    # With a second admin it becomes allowed.
    users.create_user("admin2", "secret1", "admin")
    users.set_role("admin1", "viewer")
    assert users.get_user("admin1")["role"] == "viewer"


def test_set_password_changes_login():
    users.ensure_initialised()
    users.create_user("kid", "secret1", "operator")
    users.set_password("kid", "newpass1")
    assert users.authenticate("kid", "secret1") is False
    assert users.authenticate("kid", "newpass1") is True


def test_custom_role_crud():
    users.ensure_initialised()
    users.create_role("printonly", ["view", "print.queue", "print.start"], label="Print only")
    r = next(r for r in users.list_roles() if r["name"] == "printonly")
    assert set(r["permissions"]) == {"view", "print.queue", "print.start"}
    users.update_role("printonly", permissions=["view"])
    r = next(r for r in users.list_roles() if r["name"] == "printonly")
    assert r["permissions"] == ["view"]
    users.delete_role("printonly")
    assert all(r["name"] != "printonly" for r in users.list_roles())


def test_unknown_permissions_are_dropped():
    users.ensure_initialised()
    users.create_role("weird", ["view", "not.a.perm", "print.start"])
    r = next(r for r in users.list_roles() if r["name"] == "weird")
    assert set(r["permissions"]) == {"view", "print.start"}


def test_admin_role_is_protected():
    users.ensure_initialised()
    with pytest.raises(ValueError):
        users.delete_role("admin")
    # Editing admin's permissions is a no-op; it keeps everything.
    users.update_role("admin", permissions=["view"])
    users.create_user("root", "secret1", "admin")
    assert users.permissions_for("root") == set(users.PERMISSION_KEYS)


def test_cannot_delete_role_in_use():
    users.ensure_initialised()
    users.create_role("printonly", ["view"])
    users.create_user("kid", "secret1", "printonly")
    with pytest.raises(ValueError):
        users.delete_role("printonly")
    users.delete_user("kid")
    users.delete_role("printonly")


def test_printer_visibility_defaults_to_all():
    users.ensure_initialised()
    users.create_user("kid", "secret1", "operator")
    assert users.visible_printer_ids("kid", ["p1", "p2"]) == {"p1", "p2"}
    assert users.can_see_printer("kid", "p1") is True


def test_role_printer_restriction():
    users.ensure_initialised()
    users.create_role("shopa", ["view", "print.start"], printers=["p1"])
    users.create_user("kid", "secret1", "shopa")
    assert users.visible_printer_ids("kid", ["p1", "p2", "p3"]) == {"p1"}
    assert users.can_see_printer("kid", "p1") is True
    assert users.can_see_printer("kid", "p2") is False


def test_user_override_beats_role():
    users.ensure_initialised()
    users.create_role("shopa", ["view"], printers=["p1"])
    users.create_user("kid", "secret1", "shopa", printers=["p2", "p3"])
    assert users.visible_printer_ids("kid", ["p1", "p2", "p3"]) == {"p2", "p3"}
    # Clearing the override falls back to the role.
    users.set_user_printers("kid", None)
    assert users.visible_printer_ids("kid", ["p1", "p2", "p3"]) == {"p1"}


def test_user_override_can_be_all():
    users.ensure_initialised()
    users.create_role("shopa", ["view"], printers=["p1"])
    users.create_user("kid", "secret1", "shopa")
    users.set_user_printers("kid", users.ALL_PRINTERS)
    assert users.visible_printer_ids("kid", ["p1", "p2"]) == {"p1", "p2"}


def test_admin_sees_all_printers():
    users.ensure_initialised()
    users.create_user("boss", "secret1", "admin")
    assert users.visible_printer_ids("boss", ["p1", "p2"]) == {"p1", "p2"}


def test_can_cancel_job_rules():
    users.ensure_initialised()
    users.create_user("kid", "secret1", "operator")      # has job.cancel_own
    users.create_user("boss", "secret1", "admin")        # has job.cancel_any
    users.create_user("gran", "secret1", "viewer")       # has neither
    assert users.can_cancel_job("kid", "kid") is True
    assert users.can_cancel_job("kid", "someone_else") is False
    assert users.can_cancel_job("kid", None) is False
    assert users.can_cancel_job("boss", "kid") is True
    assert users.can_cancel_job("gran", "gran") is False
