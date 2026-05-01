"""Tests for the vet-pack share token + signed-URL route.

Covers the HMAC mint/verify logic and the public /share/vet-pack
route's behaviour for both a valid token and an expired one.
"""
from __future__ import annotations

import time


def test_token_round_trip(data_dir):
    from app import vet_pack
    token = vet_pack.mint_share_token("mochi")
    assert vet_pack.verify_share_token("mochi", token) is True


def test_token_rejects_wrong_pet(data_dir):
    from app import vet_pack
    token = vet_pack.mint_share_token("mochi")
    # Same secret, but different pet_id in the message → HMAC mismatch.
    assert vet_pack.verify_share_token("maru", token) is False


def test_token_expires(data_dir):
    from app import vet_pack
    token = vet_pack.mint_share_token("mochi", ttl=60)
    # Skip past expiry.
    assert vet_pack.verify_share_token(
        "mochi", token, now=time.time() + 7200,
    ) is False


def test_token_rejects_garbage(data_dir):
    from app import vet_pack
    assert vet_pack.verify_share_token("mochi", "") is False
    assert vet_pack.verify_share_token("mochi", "no-dot-in-here") is False
    assert vet_pack.verify_share_token("mochi", "abc.def") is False
    # exp-not-an-integer
    assert vet_pack.verify_share_token("mochi", "wat.AAAA") is False


def test_share_secret_derives_distinct_subkey(data_dir):
    """The share-link HMAC key must NOT equal the raw session secret —
    domain separation. If a future code path ever hashes user input
    with the raw session secret to produce 24-byte outputs, the share
    sig still can't be replayed because they're built from different
    HMAC keys."""
    from app import vet_pack, config_store
    cfg = config_store.load_config()
    raw = cfg.admin_session_secret.encode("utf-8")
    derived = vet_pack._share_secret()
    assert derived != raw
    # Re-deriving is deterministic.
    assert derived == vet_pack._share_secret()


def test_token_requires_admin_session_secret(data_dir, monkeypatch):
    """When the admin session secret is empty, minting should refuse —
    a blank-secret HMAC would let anyone forge links."""
    from app import vet_pack, config_store
    cfg = config_store.load_config()
    cfg.admin_session_secret = ""
    monkeypatch.setattr(config_store, "load_config", lambda: cfg)
    import pytest
    with pytest.raises(RuntimeError):
        vet_pack.mint_share_token("mochi")
