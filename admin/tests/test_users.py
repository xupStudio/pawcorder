"""Tests for multi-user + role-based access."""
from __future__ import annotations

import pytest


def test_create_user(data_dir):
    from app import users
    u = users.create_user("ivan", "supersecret", "admin")
    assert u.username == "ivan"
    assert u.role == "admin"
    assert u.pw_hash != "supersecret"  # never stored plain


def test_create_rejects_short_password(data_dir):
    from app import users
    with pytest.raises(users.UserError):
        users.create_user("x", "short", "family")


def test_create_rejects_bad_username(data_dir):
    from app import users
    with pytest.raises(users.UserError):
        users.create_user("with space", "supersecret", "family")
    with pytest.raises(users.UserError):
        users.create_user("x" * 40, "supersecret", "family")


def test_create_rejects_bad_role(data_dir):
    from app import users
    with pytest.raises(users.UserError):
        users.create_user("ivan", "supersecret", "demigod")


def test_authenticate_round_trip(data_dir):
    from app import users
    users.create_user("ivan", "supersecret", "admin")
    assert users.authenticate("ivan", "supersecret") is not None
    assert users.authenticate("ivan", "wrong") is None
    assert users.authenticate("ghost", "x") is None


def test_change_password(data_dir):
    from app import users
    users.create_user("ivan", "supersecret", "admin")
    users.change_password("ivan", "newsecret123")
    assert users.authenticate("ivan", "supersecret") is None
    assert users.authenticate("ivan", "newsecret123") is not None


def test_change_role(data_dir):
    from app import users
    users.create_user("ivan", "supersecret", "admin")
    users.create_user("kid", "kidpassword", "family")
    users.change_role("kid", "kid")
    assert users.get_user("kid").role == "kid"


def test_cannot_demote_last_admin(data_dir):
    from app import users
    users.create_user("ivan", "supersecret", "admin")
    with pytest.raises(users.UserError, match="last admin"):
        users.change_role("ivan", "family")


def test_cannot_delete_last_admin(data_dir):
    """If we let the last admin go, no one can manage users anymore.
    Refuse and tell the user to add another admin first."""
    from app import users
    users.create_user("ivan", "supersecret", "admin")
    users.create_user("kid", "kidpass1234", "kid")
    with pytest.raises(users.UserError, match="last admin"):
        users.delete_user("ivan")
    # After demotion-blocked, the kid should still be deletable.
    assert users.delete_user("kid") is True


def test_has_users_starts_false(data_dir):
    from app import users
    assert users.has_users() is False


def test_has_role_rank(data_dir):
    from app import users
    assert users.has_role("admin", "kid") is True
    assert users.has_role("kid", "admin") is False
    assert users.has_role("family", "kid") is True
    assert users.has_role("family", "admin") is False
    assert users.has_role(None, "kid") is False


# ---- routes ------------------------------------------------------------

def test_users_create_route(authed_client):
    resp = authed_client.post("/api/users", json={
        "username": "ivan", "password": "supersecret", "role": "admin",
    })
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "admin"


def test_users_list_route_admin_only(authed_client):
    resp = authed_client.get("/api/users")
    assert resp.status_code == 200
    body = resp.json()
    assert "users" in body
    assert "legacy_mode" in body


def test_users_me_route(authed_client):
    """In legacy single-password mode, the session is treated as admin."""
    resp = authed_client.get("/api/users/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "admin"


def test_login_route_multi_user(app_client, data_dir):
    """Once users.yml has entries, login requires username + password."""
    from app import users
    users.create_user("ivan", "supersecret", "admin")
    # Login flow: post username + password.
    resp = app_client.post("/login", data={
        "username": "ivan", "password": "supersecret",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_login_route_multi_user_wrong_password(app_client, data_dir):
    from app import users
    users.create_user("ivan", "supersecret", "admin")
    resp = app_client.post("/login", data={
        "username": "ivan", "password": "wrong",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=invalid" in resp.headers["location"]
