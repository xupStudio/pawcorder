"""Tests for API key store + bearer auth integration."""
from __future__ import annotations

import pytest


def test_create_returns_plain_once(data_dir):
    from app import api_keys
    plain, record = api_keys.create_key("Home Assistant")
    assert plain.startswith("pwc_")
    assert record.name == "Home Assistant"
    assert record.key_id == record.sha256_hex[:8]


def test_create_does_not_store_plain(data_dir):
    """The on-disk file must contain only hashes — never the plain key."""
    from app import api_keys
    plain, _ = api_keys.create_key("test")
    text = api_keys.KEYS_FILE.read_text()
    # The plain key is base64url of 32 random bytes — at minimum the
    # tail won't appear verbatim. Check for a slice.
    assert plain[8:24] not in text


def test_verify_bearer_match(data_dir):
    from app import api_keys
    plain, record = api_keys.create_key("HA")
    found = api_keys.verify_bearer(plain)
    assert found is not None
    assert found.key_id == record.key_id


def test_verify_bearer_no_match(data_dir):
    from app import api_keys
    api_keys.create_key("HA")
    assert api_keys.verify_bearer("pwc_bogus_token") is None


def test_revoke_removes_key(data_dir):
    from app import api_keys
    plain, record = api_keys.create_key("ha")
    assert api_keys.revoke_key(record.key_id) is True
    assert api_keys.verify_bearer(plain) is None


def test_revoke_unknown_returns_false(data_dir):
    from app import api_keys
    assert api_keys.revoke_key("notreal") is False


def test_list_public_strips_hash(data_dir):
    from app import api_keys
    api_keys.create_key("HA")
    public = api_keys.list_keys_public()
    assert len(public) == 1
    assert "sha256_hex" not in public[0]
    assert public[0]["preview"].startswith("pwc_…")


# ---- routes ------------------------------------------------------------

def test_api_keys_route_create_and_revoke(authed_client):
    resp = authed_client.post("/api/system/api-keys", json={"name": "test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"].startswith("pwc_")
    key_id = body["record"]["key_id"]

    # List shows it
    listing = authed_client.get("/api/system/api-keys").json()["keys"]
    assert any(k["key_id"] == key_id for k in listing)

    # Revoke
    resp = authed_client.delete(f"/api/system/api-keys/{key_id}")
    assert resp.status_code == 200
    assert authed_client.get("/api/system/api-keys").json()["keys"] == []


def test_bearer_token_replaces_session(app_client):
    """A request with a valid Authorization: Bearer must succeed
    without a session cookie AND without the X-Requested-With header."""
    from app import api_keys
    plain, _ = api_keys.create_key("ci")
    # No session cookie, no CSRF header
    resp = app_client.get(
        "/api/status",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200


def test_bearer_works_for_mutations_too(app_client):
    """API key bypasses CSRF — that's the whole point. POST should work."""
    from app import api_keys
    plain, _ = api_keys.create_key("ci")
    resp = app_client.post(
        "/api/privacy",
        headers={"Authorization": f"Bearer {plain}", "X-Requested-With": ""},
        json={"enabled": True},
    )
    assert resp.status_code == 200


def test_bearer_bad_token_falls_through_to_session_check(app_client):
    """A bad bearer token isn't an automatic 401 — we fall through to
    cookie auth, which then 401s. (Security property: an attacker
    can't probe valid keys via the response timing because both
    paths return the same 401.)"""
    resp = app_client.get(
        "/api/status",
        headers={"Authorization": "Bearer not-real"},
    )
    assert resp.status_code == 401
