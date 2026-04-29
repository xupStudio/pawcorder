"""Updater + version comparison tests."""
from __future__ import annotations

import pytest


def test_normalize_handles_v_prefix(data_dir):
    from app.updater import _normalize
    assert _normalize("v1.2.3") == _normalize("1.2.3") == (1, 2, 3)


def test_normalize_handles_pre_release(data_dir):
    from app.updater import _normalize
    # 'rc1' suffix is stripped before parsing
    assert _normalize("1.2.3-rc1") == (1, 2, 3)


def test_normalize_pads_short_versions(data_dir):
    from app.updater import _normalize
    assert _normalize("2") == (2, 0, 0)
    assert _normalize("2.5") == (2, 5, 0)


def test_normalize_handles_garbage(data_dir):
    from app.updater import _normalize
    # Non-numeric segments become 0 — we'd rather underflag than crash
    assert _normalize("banana") == (0, 0, 0)


def test_is_newer_dev_returns_false(data_dir):
    """A dev build shouldn't see itself as out-of-date."""
    from app.updater import is_newer
    assert not is_newer("v1.0.0", "dev")


def test_is_newer_basic(data_dir):
    from app.updater import is_newer
    assert is_newer("v1.0.1", "v1.0.0")
    assert not is_newer("v1.0.0", "v1.0.0")
    assert not is_newer("v0.9.9", "v1.0.0")


def test_current_version_reads_file(data_dir):
    from app import updater
    # In the source tree there's a VERSION file. Reading it should not crash.
    v = updater.current_version()
    assert isinstance(v, str)


def test_update_check_route_works(authed_client, monkeypatch):
    """When GitHub is unreachable, route returns soft-fail rather than 500."""
    from app import updater

    async def _boom(*_, **__):
        raise RuntimeError("offline")

    monkeypatch.setattr(updater, "fetch_latest_release", _boom)
    monkeypatch.setattr(updater, "_cache", {"checked_at": 0.0, "result": None})

    resp = authed_client.get("/api/system/update-check?force=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["update_available"] is False
    assert body["error"]


def test_update_check_route_finds_newer(authed_client, monkeypatch):
    from app import updater

    async def _ok(*_, **__):
        return {
            "tag_name": "v999.0.0",
            "html_url": "https://example.com/r/v999.0.0",
            "body": "pretend release notes",
        }

    monkeypatch.setattr(updater, "fetch_latest_release", _ok)
    # Pretend we're a real release, not a dev build.
    monkeypatch.setattr(updater, "current_version", lambda: "0.1.0")
    monkeypatch.setattr(updater, "_cache", {"checked_at": 0.0, "result": None})

    resp = authed_client.get("/api/system/update-check?force=true")
    body = resp.json()
    assert body["update_available"] is True
    assert body["latest_version"] == "v999.0.0"


def test_system_version_route(authed_client):
    resp = authed_client.get("/api/system/version")
    assert resp.status_code == 200
    assert "version" in resp.json()
