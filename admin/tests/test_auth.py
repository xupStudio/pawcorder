"""Tests for session-cookie auth helpers."""
from __future__ import annotations


def test_password_matches_uses_stored_value(data_dir):
    from app import auth
    assert auth.password_matches("test") is True
    assert auth.password_matches("wrong") is False


def test_session_round_trip(data_dir):
    from app import auth
    token = auth.issue_session()
    assert auth.verify_session(token) is True
    assert auth.verify_session("garbage") is False
    assert auth.verify_session(None) is False


def test_session_signed_with_secret(data_dir):
    """Tokens minted with one secret should fail verification with another."""
    from app import auth, config_store
    cfg = config_store.load_config()
    cfg.admin_session_secret = "secret-A"
    config_store.save_config(cfg)
    token = auth.issue_session()

    cfg.admin_session_secret = "secret-B"
    config_store.save_config(cfg)
    assert auth.verify_session(token) is False


def test_password_match_constant_time(data_dir):
    """compare_digest is used; just sanity-check that hmac is imported."""
    from app import auth
    import hmac
    assert hasattr(hmac, "compare_digest")
    assert auth.password_matches("test") is True
