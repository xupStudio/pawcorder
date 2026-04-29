"""Tests for NAS mount helper. We never actually call mount(8) — that
needs CAP_SYS_ADMIN and a real NFS/SMB server. Instead we exercise the
validation + fstab-text manipulation logic, which is where bugs hide
(e.g. corrupting unrelated /etc/fstab lines on re-install).
"""
from __future__ import annotations

import os

import pytest


def test_validate_rejects_bad_protocol(data_dir):
    from app import nas_mount
    cfg = nas_mount.MountConfig(
        protocol="ftp", server="1.2.3.4", share="/x", mount_point="/mnt/x",
    )
    err = nas_mount.validate(cfg)
    assert err and "protocol" in err


def test_validate_requires_server(data_dir):
    from app import nas_mount
    cfg = nas_mount.MountConfig(
        protocol="nfs", server="", share="/x", mount_point="/mnt/x",
    )
    assert nas_mount.validate(cfg) == "server is required"


def test_validate_requires_share(data_dir):
    from app import nas_mount
    cfg = nas_mount.MountConfig(
        protocol="nfs", server="1.2.3.4", share="", mount_point="/mnt/x",
    )
    assert nas_mount.validate(cfg) == "share path is required"


def test_validate_requires_absolute_mount_point(data_dir):
    from app import nas_mount
    cfg = nas_mount.MountConfig(
        protocol="nfs", server="1.2.3.4", share="/x", mount_point="relative/path",
    )
    err = nas_mount.validate(cfg)
    assert err and "absolute" in err


def test_validate_smb_requires_username(data_dir):
    from app import nas_mount
    cfg = nas_mount.MountConfig(
        protocol="smb", server="srv", share="/share", mount_point="/mnt/x",
        username="", password="",
    )
    err = nas_mount.validate(cfg)
    assert err and "username" in err


def test_validate_passes_for_valid_nfs(data_dir):
    from app import nas_mount
    cfg = nas_mount.MountConfig(
        protocol="nfs", server="1.2.3.4", share="/volume1/cameras",
        mount_point="/mnt/pawcorder",
    )
    assert nas_mount.validate(cfg) is None


def test_strip_existing_pawcorder_lines_idempotent(data_dir):
    from app import nas_mount
    initial = (
        "# original system entries\n"
        "/dev/sda1 / ext4 defaults 0 0\n"
        "\n"
        "# pawcorder-nas-mount (do not edit by hand)\n"
        "1.2.3.4:/x /mnt/x nfs defaults 0 0\n"
        "\n"
        "# unrelated trailing entry\n"
        "tmpfs /tmp tmpfs defaults 0 0\n"
    )
    cleaned = nas_mount._strip_existing_pawcorder_lines(initial)
    # marker + the line right after must be gone
    assert "pawcorder-nas-mount" not in cleaned
    assert "1.2.3.4:/x" not in cleaned
    # but the unrelated user lines must survive
    assert "/dev/sda1 / ext4" in cleaned
    assert "tmpfs /tmp tmpfs" in cleaned


def test_strip_handles_no_marker(data_dir):
    from app import nas_mount
    text = "/dev/sda1 / ext4 defaults 0 0\n"
    assert nas_mount._strip_existing_pawcorder_lines(text) == text.rstrip("\n")


def test_install_to_fstab_writes_nfs_line(data_dir, tmp_path, monkeypatch):
    """Re-point FSTAB_PATH at a tmp file so we don't smash /etc/fstab."""
    from app import nas_mount
    fake_fstab = tmp_path / "fstab"
    fake_fstab.write_text("/dev/sda1 / ext4 defaults 0 0\n", encoding="utf-8")
    monkeypatch.setattr(nas_mount, "FSTAB_PATH", fake_fstab)

    cfg = nas_mount.MountConfig(
        protocol="nfs", server="10.0.0.5", share="/volume1/pets",
        mount_point="/mnt/pawcorder",
    )
    err = nas_mount.install_to_fstab(cfg)
    assert err is None
    body = fake_fstab.read_text(encoding="utf-8")
    assert "pawcorder-nas-mount" in body
    assert "10.0.0.5:/volume1/pets" in body
    assert "/mnt/pawcorder" in body
    # Original lines preserved.
    assert "/dev/sda1 / ext4" in body


def test_install_to_fstab_replaces_old_entry(data_dir, tmp_path, monkeypatch):
    """A second install should not duplicate lines or corrupt existing ones."""
    from app import nas_mount
    fake_fstab = tmp_path / "fstab"
    fake_fstab.write_text("/dev/sda1 / ext4 defaults 0 0\n", encoding="utf-8")
    monkeypatch.setattr(nas_mount, "FSTAB_PATH", fake_fstab)

    cfg1 = nas_mount.MountConfig(
        protocol="nfs", server="10.0.0.5", share="/old",
        mount_point="/mnt/pawcorder",
    )
    nas_mount.install_to_fstab(cfg1)
    cfg2 = nas_mount.MountConfig(
        protocol="nfs", server="10.0.0.6", share="/new",
        mount_point="/mnt/pawcorder",
    )
    nas_mount.install_to_fstab(cfg2)
    body = fake_fstab.read_text(encoding="utf-8")
    assert body.count("pawcorder-nas-mount") == 1
    assert "/old" not in body
    assert "/new" in body


def test_install_to_fstab_smb_writes_credentials(data_dir, tmp_path, monkeypatch):
    from app import nas_mount
    fake_fstab = tmp_path / "fstab"
    fake_fstab.write_text("/dev/sda1 / ext4 defaults 0 0\n", encoding="utf-8")
    creds_path = tmp_path / "smb.creds"
    monkeypatch.setattr(nas_mount, "FSTAB_PATH", fake_fstab)

    cfg = nas_mount.MountConfig(
        protocol="smb", server="nas.local", share="/cameras",
        mount_point="/mnt/pawcorder", username="paw", password="hunter2",
    )
    err = nas_mount.install_to_fstab(cfg, smb_credentials_path=str(creds_path))
    assert err is None
    creds_text = creds_path.read_text(encoding="utf-8")
    assert "username=paw" in creds_text
    assert "password=hunter2" in creds_text
    # 0600 — never world-readable.
    mode = oct(creds_path.stat().st_mode)[-3:]
    assert mode == "600"
    # fstab entry references the creds file by path, not the password.
    body = fake_fstab.read_text(encoding="utf-8")
    assert "hunter2" not in body
    assert f"credentials={creds_path}" in body


def test_install_to_fstab_errors_on_missing_file(data_dir, tmp_path, monkeypatch):
    from app import nas_mount
    monkeypatch.setattr(nas_mount, "FSTAB_PATH", tmp_path / "does-not-exist")
    cfg = nas_mount.MountConfig(
        protocol="nfs", server="x", share="/x", mount_point="/mnt/x",
    )
    err = nas_mount.install_to_fstab(cfg)
    assert err and "not found" in err


# ---- routes ------------------------------------------------------------

def test_storage_test_mount_route_validates(authed_client, monkeypatch):
    """The route should surface MountTestResult via JSON."""
    from app import nas_mount

    async def _fake_test(cfg, *, timeout=15):
        return nas_mount.MountTestResult(ok=True, message="mount OK", output="")

    monkeypatch.setattr(nas_mount, "test_mount", _fake_test)
    resp = authed_client.post("/api/storage/test-mount", json={
        "protocol": "nfs", "server": "1.2.3.4",
        "share": "/v1/cams", "mount_point": "/mnt/pawcorder",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


def test_storage_test_mount_requires_csrf(app_client, data_dir):
    """A request without CSRF (X-Requested-With) and without auth should be rejected."""
    resp = app_client.post(
        "/api/storage/test-mount",
        json={"protocol": "nfs", "server": "x", "share": "/x"},
        headers={"X-Requested-With": ""},
    )
    # Either 401 (auth) or 403 (CSRF) is acceptable — either way, not 200.
    assert resp.status_code in (401, 403)
