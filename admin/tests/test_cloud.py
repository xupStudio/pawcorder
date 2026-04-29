"""Tests for cloud (rclone wrapper) module."""
from __future__ import annotations

import pytest


def test_save_and_load_remote(data_dir):
    from app import cloud
    cloud.save_remote("mydrive", {"type": "drive", "scope": "drive", "token": "{...}"})
    assert "mydrive" in cloud.list_remotes()
    fields = cloud.get_remote("mydrive")
    assert fields["type"] == "drive"
    assert fields["token"] == "{...}"


def test_save_remote_requires_type(data_dir):
    from app import cloud
    with pytest.raises(ValueError):
        cloud.save_remote("badremote", {"scope": "drive"})


def test_delete_remote(data_dir):
    from app import cloud
    cloud.save_remote("r1", {"type": "drive", "token": "x"})
    assert cloud.delete_remote("r1") is True
    assert cloud.delete_remote("r1") is False


def test_replace_remote(data_dir):
    from app import cloud
    cloud.save_remote("dup", {"type": "drive", "token": "first"})
    cloud.save_remote("dup", {"type": "drive", "token": "second"})
    assert cloud.get_remote("dup")["token"] == "second"


def test_rclone_conf_file_mode_600(data_dir):
    """The config holds OAuth tokens, so it must be private."""
    from app import cloud
    cloud.save_remote("private", {"type": "dropbox", "token": "xyz"})
    import stat
    mode = cloud.RCLONE_CONFIG_PATH.stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.parametrize("backend, payload, expected_keys", [
    ("drive",    {"token": "T"}, {"type", "scope", "token"}),
    ("dropbox",  {"token": "T"}, {"type", "token"}),
    ("onedrive", {"token": "T", "drive_id": "D"}, {"type", "drive_id", "drive_type", "token"}),
    ("b2",       {"account": "A", "key": "K"}, {"type", "account", "key"}),
    ("s3",       {"access_key_id": "A", "secret_access_key": "S", "endpoint": "E"},
                 {"type", "provider", "endpoint", "access_key_id", "secret_access_key", "region"}),
    ("webdav",   {"url": "U", "user": "u", "pass": "p"},
                 {"type", "url", "vendor", "user", "pass"}),
])
def test_fields_for_backend_filters_unknown(data_dir, backend, payload, expected_keys):
    from app.cloud import fields_for_backend
    payload_with_garbage = {**payload, "evil": "should not appear"}
    out = fields_for_backend(backend, payload_with_garbage)
    assert "evil" not in out
    assert set(out.keys()) == expected_keys


def test_fields_for_backend_unknown_raises(data_dir):
    from app.cloud import fields_for_backend
    with pytest.raises(ValueError):
        fields_for_backend("frobnicator", {})


def test_test_remote_for_unknown_returns_failure(data_dir):
    """Async test helper: bare-bones — just checks the path that returns
    a not-configured RcloneTestResult without invoking rclone."""
    import asyncio
    from app import cloud
    result = asyncio.run(cloud.test_remote("ghost"))
    assert result.ok is False
    assert "not configured" in result.detail


def test_estimate_max_for_free_space():
    from app.cloud import estimate_max_for_free_space
    assert estimate_max_for_free_space(0) == 0
    assert estimate_max_for_free_space(100) == 80          # 80% default
    assert estimate_max_for_free_space(1000, 0.5) == 500   # custom fraction
    assert estimate_max_for_free_space(-5) == 0


def test_get_quota_unconfigured(data_dir):
    """Asking for quota of a non-existent remote returns unsupported + error."""
    import asyncio
    from app import cloud
    result = asyncio.run(cloud.get_quota("ghost", "pawcorder"))
    assert result.quota_supported is False
    assert "not configured" in result.error


def test_get_quota_parses_about_json(data_dir, monkeypatch):
    """When `rclone about` returns valid JSON, get_quota should parse it."""
    import asyncio
    from app import cloud
    cloud.save_remote("test", {"type": "drive", "token": "x"})

    calls = []
    async def fake_run(*args, timeout=30):
        calls.append(args)
        if "about" in args:
            return 0, '{"total":214748364800,"used":53687091200,"free":161061273600}', ""
        if "size" in args:
            return 0, '{"count":3,"bytes":5368709120}', ""
        return 1, "", "unexpected"

    monkeypatch.setattr(cloud, "_run_rclone", fake_run)
    result = asyncio.run(cloud.get_quota("test", "pawcorder"))
    assert result.quota_supported is True
    assert result.total_bytes == 214748364800   # 200 GB
    assert result.free_bytes == 161061273600
    assert result.pawcorder_bytes == 5368709120  # 5 GB


def test_get_quota_unsupported_backend(data_dir, monkeypatch):
    """B2 / S3 don't report quota — about call fails or returns nothing."""
    import asyncio
    from app import cloud
    cloud.save_remote("b2cam", {"type": "b2", "account": "a", "key": "k"})

    async def fake_run(*args, timeout=30):
        if "about" in args:
            return 1, "", "command about not supported by backend"
        if "size" in args:
            return 0, '{"count":1,"bytes":1024}', ""
        return 1, "", "unexpected"

    monkeypatch.setattr(cloud, "_run_rclone", fake_run)
    result = asyncio.run(cloud.get_quota("b2cam", "pawcorder"))
    assert result.quota_supported is False
    assert result.pawcorder_bytes == 1024


def test_prune_oldest_until_under(data_dir, monkeypatch):
    """Verify prune walks the file list oldest-first and stops at target."""
    import asyncio
    from app import cloud
    cloud.save_remote("test", {"type": "drive", "token": "x"})

    files_json = """[
      {"Path": "old.mp4",    "Size": 1000, "ModTime": "2024-01-01T00:00:00Z"},
      {"Path": "middle.mp4", "Size": 1000, "ModTime": "2024-06-01T00:00:00Z"},
      {"Path": "new.mp4",    "Size": 1000, "ModTime": "2024-12-01T00:00:00Z"}
    ]"""
    deleted_paths: list[str] = []

    async def fake_run(*args, timeout=30):
        if args[0] == "lsjson":
            return 0, files_json, ""
        if args[0] == "deletefile":
            deleted_paths.append(args[1])
            return 0, "", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(cloud, "_run_rclone", fake_run)
    # Total is 3000 bytes; cap at 1500 means we need to remove 1500+ — the
    # oldest file is 1000 bytes (still over), so two will go.
    deleted = asyncio.run(cloud.prune_oldest_until_under("test", "pawcorder", 1500))
    assert deleted == 2
    # Oldest should be deleted first.
    assert deleted_paths[0].endswith("old.mp4")
    assert deleted_paths[1].endswith("middle.mp4")


def test_write_config_is_atomic(data_dir, monkeypatch):
    """Crash between rclone.conf write and rename must leave the
    previous tokens intact. OAuth tokens can't be regenerated without
    re-running `rclone authorize` from scratch."""
    from app import cloud
    from app import utils

    # Establish a known-good rclone.conf with a token we can recover.
    cloud.save_remote("mydrive", {"type": "drive", "token": "ORIGINAL_TOKEN"})
    assert cloud.get_remote("mydrive")["token"] == "ORIGINAL_TOKEN"

    # Sabotage os.replace so the next write crashes between truncate
    # and commit.
    def _boom(*_args, **_kw):
        raise OSError("simulated kill")
    monkeypatch.setattr(utils.os, "replace", _boom)

    with pytest.raises(OSError):
        cloud.save_remote("mydrive", {"type": "drive", "token": "OVERWRITE"})

    # rclone.conf must still hold the original token — atomic guarantee.
    assert cloud.get_remote("mydrive")["token"] == "ORIGINAL_TOKEN"
