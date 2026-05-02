"""Family invite link flow."""
from __future__ import annotations

import os
import time

import pytest


@pytest.fixture(autouse=True)
def _clean_invites(tmp_path, monkeypatch):
    """Each test gets its own invites.yml so runs don't leak state."""
    monkeypatch.setenv("PAWCORDER_DATA_DIR", str(tmp_path))
    # Re-import after env change so module-level INVITES_FILE picks up the
    # new path. Stash + restore so the rest of the suite isn't affected.
    import importlib
    from app import invites as invites_module
    importlib.reload(invites_module)
    yield invites_module
    # Reload again with original env for cleanup
    importlib.reload(invites_module)


def test_create_returns_plaintext_token_once(_clean_invites):
    inv = _clean_invites
    token, rec = inv.create(role="family", created_by="admin")
    assert isinstance(token, str)
    assert len(token) >= 30  # token_urlsafe(24) — at least 32 chars
    assert rec.role == "family"
    assert rec.created_by == "admin"
    assert rec.is_active()
    # Plaintext is never persisted — only its hash.
    assert token not in (rec.token_hash + str(rec))


def test_redeem_consumes_invite_once(_clean_invites):
    inv = _clean_invites
    token, _rec = inv.create(role="family", created_by="admin")
    consumed = inv.consume(token, used_by_username="alice")
    assert consumed.is_used()
    # Second redeem must fail — single-use.
    with pytest.raises(inv.InviteError, match="already used"):
        inv.consume(token, used_by_username="bob")


def test_redeem_with_unknown_token_fails(_clean_invites):
    inv = _clean_invites
    with pytest.raises(inv.InviteError, match="not found"):
        inv.consume("not-a-real-token", used_by_username="alice")


def test_expired_invite_not_active_and_not_redeemable(_clean_invites, monkeypatch):
    inv = _clean_invites
    token, _rec = inv.create(role="family", created_by="admin", ttl_secs=60)
    # Move "now" forward past expiry.
    real_time = inv.time.time
    monkeypatch.setattr(inv.time, "time", lambda: real_time() + 120)
    assert inv.find_active(token) is None
    with pytest.raises(inv.InviteError, match="expired"):
        inv.consume(token, used_by_username="alice")


def test_revoke_drops_by_public_id(_clean_invites):
    inv = _clean_invites
    _token, rec = inv.create(role="family", created_by="admin")
    public_id = rec.token_hash[:8]
    assert inv.revoke(public_id) is True
    assert len(inv.list_active()) == 0
    # Revoking again is a no-op (404 in the route layer).
    assert inv.revoke(public_id) is False


def test_admin_role_invite_rejected(_clean_invites):
    inv = _clean_invites
    with pytest.raises(inv.InviteError, match="must be 'family' or 'kid'"):
        inv.create(role="admin", created_by="admin")


def test_find_active_does_not_mutate(_clean_invites):
    inv = _clean_invites
    token, _rec = inv.create(role="family", created_by="admin")
    found = inv.find_active(token)
    assert found is not None
    # Calling find_active again still works — no consumption.
    assert inv.find_active(token) is not None
    # The token still consumes successfully.
    inv.consume(token, used_by_username="alice")
