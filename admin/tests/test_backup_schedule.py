"""Tests for daily auto-backup scheduler + AES-GCM encryption."""
from __future__ import annotations

import pytest


# ---- encryption round-trip ---------------------------------------------

def test_encrypt_then_decrypt(data_dir):
    from app.backup_schedule import decrypt_blob, encrypt_blob
    plaintext = b"hello pawcorder backup blob bytes"
    encrypted = encrypt_blob(plaintext, "secret-password-123")
    assert encrypted.startswith(b"pwc-bkp-v1\n")
    assert encrypted != plaintext
    out = decrypt_blob(encrypted, "secret-password-123")
    assert out == plaintext


def test_decrypt_wrong_password_fails(data_dir):
    from app.backup_schedule import decrypt_blob, encrypt_blob
    encrypted = encrypt_blob(b"secret", "right")
    with pytest.raises(ValueError):
        decrypt_blob(encrypted, "wrong")


def test_decrypt_wrong_magic_fails(data_dir):
    from app.backup_schedule import decrypt_blob
    with pytest.raises(ValueError, match="not a pawcorder"):
        decrypt_blob(b"some other format", "x")


def test_decrypt_truncated_fails(data_dir):
    from app.backup_schedule import decrypt_blob
    blob = b"pwc-bkp-v1\n" + b"\x00" * 5  # too short
    with pytest.raises(ValueError, match="truncated"):
        decrypt_blob(blob, "x")


def test_encrypt_requires_password(data_dir):
    from app.backup_schedule import encrypt_blob
    with pytest.raises(ValueError):
        encrypt_blob(b"x", "")


# ---- state IO ----------------------------------------------------------

def test_state_round_trip(data_dir):
    from app import backup_schedule
    state = backup_schedule.ScheduleState(
        enabled=True, encrypt=True, encryption_password="hunter2",
        cloud_path="my/backups",
    )
    backup_schedule.save_state(state)
    loaded = backup_schedule.load_state()
    assert loaded.enabled
    assert loaded.encrypt
    assert loaded.encryption_password == "hunter2"
    assert loaded.cloud_path == "my/backups"


def test_to_dict_hides_password_by_default(data_dir):
    from app import backup_schedule
    s = backup_schedule.ScheduleState(encryption_password="topsecret")
    d = s.to_dict()
    assert "encryption_password" not in d
    assert d["password_set"] is True


# ---- run_once_now ------------------------------------------------------

def test_run_disabled_returns_error(data_dir):
    import asyncio
    from app import backup_schedule
    out = asyncio.run(backup_schedule.run_once_now())
    assert out["ok"] is False
    assert "disabled" in out["error"]


def test_run_no_remote_returns_error(data_dir):
    """Enabled but no cloud configured → friendly error, not exception."""
    import asyncio
    from app import backup_schedule

    state = backup_schedule.ScheduleState(enabled=True)
    backup_schedule.save_state(state)
    out = asyncio.run(backup_schedule.run_once_now())
    assert out["ok"] is False
    assert "no cloud remote" in out["error"]


def test_run_encryption_without_password_errors(data_dir):
    import asyncio
    from app import backup_schedule, cloud
    cloud.save_remote("pawcorder", {"type": "drive", "token": "t"})
    state = backup_schedule.ScheduleState(enabled=True, encrypt=True)
    backup_schedule.save_state(state)
    out = asyncio.run(backup_schedule.run_once_now())
    assert out["ok"] is False
    assert "password" in out["error"]


# ---- routes ------------------------------------------------------------

def test_schedule_route_get(authed_client):
    resp = authed_client.get("/api/backup/schedule")
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body
    assert "password_set" in body


def test_schedule_route_save(authed_client):
    resp = authed_client.post("/api/backup/schedule", json={
        "enabled": True, "encrypt": True, "encryption_password": "pw",
    })
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert resp.json()["password_set"] is True


def test_schedule_route_clear_password(authed_client):
    """The clear_password flag wipes the stored password explicitly."""
    authed_client.post("/api/backup/schedule", json={
        "encryption_password": "secret",
    })
    resp = authed_client.post("/api/backup/schedule", json={
        "clear_password": True,
    })
    assert resp.json()["password_set"] is False
