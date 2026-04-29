"""Backup / restore round-trip tests."""
from __future__ import annotations

from pathlib import Path


def test_backup_round_trip(data_dir):
    """Make a backup, mutate, restore, original content returns."""
    from app import backup as backup_mod

    blob = backup_mod.make_backup()
    assert isinstance(blob, bytes) and len(blob) > 100

    meta = backup_mod.inspect_backup(blob)
    assert meta["version"] == backup_mod.BACKUP_VERSION
    assert ".env" in meta["files"]

    # Mutate the env file.
    env_path = data_dir / ".env"
    original = env_path.read_text()
    env_path.write_text("STORAGE_PATH=\"junk\"\n")

    result = backup_mod.restore_backup(blob)
    assert result.ok, result.error
    assert result.files_restored >= 1
    assert env_path.read_text() == original


def test_backup_includes_cameras_and_rclone(data_dir):
    """Cameras and rclone.conf, when present, appear in the backup."""
    from app import backup as backup_mod

    (data_dir / "config" / "cameras.yml").write_text(
        "cameras:\n  - name: cam\n    ip: 1.1.1.1\n    user: a\n    password: p\n"
        "    rtsp_port: 554\n    onvif_port: 8000\n"
        "    detect_width: 640\n    detect_height: 480\n"
        "    enabled: true\n    connection_type: wired\n"
    )
    (data_dir / "config" / "rclone").mkdir()
    (data_dir / "config" / "rclone" / "rclone.conf").write_text("[d]\ntype = drive\n")

    blob = backup_mod.make_backup()
    meta = backup_mod.inspect_backup(blob)
    assert "config/cameras.yml" in meta["files"]
    assert "config/rclone/rclone.conf" in meta["files"]


def test_inspect_corrupt_blob_raises(data_dir):
    from app import backup as backup_mod
    import pytest

    with pytest.raises(ValueError):
        backup_mod.inspect_backup(b"not a real tar.gz")


def test_inspect_backup_returns_compatible_for_current_version(data_dir):
    """A v1 backup against a v1 build → compatible: true."""
    from app import backup as backup_mod
    blob = backup_mod.make_backup()
    meta = backup_mod.inspect_backup(blob)
    assert meta["compatible"] is True
    assert meta["expected_version"] == backup_mod.BACKUP_VERSION


def test_inspect_backup_flags_future_version_incompatible(data_dir):
    """A v999 backup against a v1 build → compatible: false (so the UI
    can disable the Restore button before the user clicks it)."""
    import io
    import json
    import tarfile

    from app import backup as backup_mod

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = json.dumps({"version": 999, "created_at": 1, "app": "pawcorder"}).encode()
        info = tarfile.TarInfo(name=backup_mod.META_FILENAME)
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))

    inspected = backup_mod.inspect_backup(buf.getvalue())
    assert inspected["version"] == 999
    assert inspected["compatible"] is False


def test_restore_rejects_unsafe_paths(data_dir, tmp_path):
    """A tarball claiming to write outside the data dir must be refused."""
    import io
    import tarfile

    from app import backup as backup_mod

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = b'{"version": 1, "created_at": 1, "app": "pawcorder"}'
        info = tarfile.TarInfo(name=backup_mod.META_FILENAME)
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))
        evil = b"PWNED"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(evil)
        tar.addfile(info, io.BytesIO(evil))

    result = backup_mod.restore_backup(buf.getvalue())
    assert not result.ok
    assert "unsafe path" in result.error or "unexpected" in result.error


def test_restore_rejects_unknown_schema(data_dir):
    """Future-version backup must refuse to restore on older code."""
    import io
    import json
    import tarfile

    from app import backup as backup_mod

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = json.dumps({"version": 999, "created_at": 1, "app": "pawcorder"}).encode()
        info = tarfile.TarInfo(name=backup_mod.META_FILENAME)
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))

    result = backup_mod.restore_backup(buf.getvalue())
    assert not result.ok
    assert "v999" in result.error or "999" in result.error


def test_humanize_bytes():
    from app import backup as backup_mod

    assert backup_mod.humanize_bytes(0) == "0 B"
    assert backup_mod.humanize_bytes(2048).endswith("KB")
    assert backup_mod.humanize_bytes(2 * 1024 * 1024).endswith("MB")


def test_backup_route_returns_gzip(authed_client):
    resp = authed_client.get("/api/backup/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content[:2] == b"\x1f\x8b"  # gzip magic


def test_backup_inspect_route(authed_client):
    download = authed_client.get("/api/backup/download")
    assert download.status_code == 200

    resp = authed_client.post(
        "/api/backup/inspect",
        files={"file": ("backup.tar.gz", download.content, "application/gzip")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert ".env" in body["files"]


def test_backup_restore_route(authed_client, data_dir):
    """Round-trip via the HTTP API.

    We mutate cameras.yml (rather than .env, which holds the session
    secret — overwriting .env mid-request would log us out and the route
    would 401 before we could test the restore behaviour itself).
    """
    # Make sure cameras.yml exists and is included in the backup.
    (data_dir / "config" / "cameras.yml").write_text(
        "cameras:\n  - name: original\n    ip: 1.1.1.1\n    user: a\n    password: p\n"
        "    rtsp_port: 554\n    onvif_port: 8000\n"
        "    detect_width: 640\n    detect_height: 480\n"
        "    enabled: true\n    connection_type: wired\n"
    )

    download = authed_client.get("/api/backup/download").content

    # Mutate cameras.yml after backup.
    (data_dir / "config" / "cameras.yml").write_text(
        "cameras:\n  - name: junk\n    ip: 9.9.9.9\n    user: x\n    password: p\n"
        "    rtsp_port: 554\n    onvif_port: 8000\n"
        "    detect_width: 640\n    detect_height: 480\n"
        "    enabled: true\n    connection_type: wired\n"
    )

    resp = authed_client.post(
        "/api/backup/restore",
        files={"file": ("backup.tar.gz", download, "application/gzip")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert "name: original" in (data_dir / "config" / "cameras.yml").read_text()
