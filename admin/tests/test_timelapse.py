"""Tests for the time-lapse module.

We don't actually call ffmpeg — that's the integration boundary the
admin can't control. We test the pieces that *can* go wrong without
ffmpeg: frame-count gating, retention pruning, listing, the "already
built" idempotency check.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


def _setup_storage(data_dir, monkeypatch, *, storage_path: str) -> Path:
    from app import config_store
    cfg = config_store.load_config()
    cfg.storage_path = storage_path
    config_store.save_config(cfg)
    return Path(storage_path)


def test_storage_root_under_configured_storage(data_dir, tmp_path):
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "store"))
    root = timelapse.storage_root()
    assert root == tmp_path / "store" / "timelapse"


def test_frames_dir_namespaces_per_camera_and_date(data_dir, tmp_path):
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "s"))
    fd = timelapse.frames_dir("2026-04-28", "front_door")
    # Path layout matters because the builder globs by camera/date.
    assert "2026-04-28" in str(fd)
    assert "front_door" in str(fd)


def test_build_one_skips_with_too_few_frames(data_dir, tmp_path):
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "s"))
    src = timelapse.frames_dir("2026-04-28", "cam1")
    src.mkdir(parents=True, exist_ok=True)
    # Only 3 frames — well below the 10-frame floor.
    for stamp in ("000000", "000100", "000200"):
        (src / f"{stamp}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
    result = timelapse.build_one("cam1", "2026-04-28")
    assert result.output is None
    assert "too few" in result.error
    # Source must NOT be deleted on a no-op skip — user might add more frames.
    assert src.exists()


def test_build_one_no_frames_returns_friendly_error(data_dir, tmp_path):
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "s"))
    result = timelapse.build_one("nonexistent_cam", "2026-04-28")
    assert result.output is None
    assert "no frames" in result.error


def test_build_one_skips_when_output_already_exists(data_dir, tmp_path, monkeypatch):
    """Re-running the builder should be safe — don't re-encode an existing reel."""
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "s"))
    out = timelapse.output_path("2026-04-28", "cam1")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"existing video")
    # Even if frame count would otherwise pass, we should short-circuit.
    src = timelapse.frames_dir("2026-04-28", "cam1")
    src.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        (src / f"{i:06d}.jpg").write_bytes(b"x")
    result = timelapse.build_one("cam1", "2026-04-28")
    assert result.output == str(out)
    assert result.error == ""
    # Existing file untouched.
    assert out.read_bytes() == b"existing video"


def test_list_timelapses_returns_newest_first(data_dir, tmp_path):
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "s"))
    root = timelapse.storage_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "cam1-2026-04-26.mp4").write_bytes(b"a")
    (root / "cam1-2026-04-27.mp4").write_bytes(b"b")
    (root / "cam1-2026-04-28.mp4").write_bytes(b"c")
    items = timelapse.list_timelapses()
    assert len(items) == 3
    # Reverse-sorted by filename — 04-28 first.
    assert items[0]["filename"] == "cam1-2026-04-28.mp4"
    assert items[0]["camera"] == "cam1"
    assert items[0]["date"] == "2026-04-28"


def test_list_timelapses_handles_missing_dir(data_dir, tmp_path):
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "doesnotexist"))
    assert timelapse.list_timelapses() == []


def test_prune_old_timelapses_drops_old_files(data_dir, tmp_path, monkeypatch):
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "s"))
    monkeypatch.setattr(timelapse, "RETENTION_DAYS", 7)
    root = timelapse.storage_root()
    root.mkdir(parents=True, exist_ok=True)

    fresh = root / "cam1-fresh.mp4"
    stale = root / "cam1-stale.mp4"
    fresh.write_bytes(b"a")
    stale.write_bytes(b"b")
    # Backdate stale: 30 days ago.
    old = time.time() - 30 * 86400
    import os
    os.utime(stale, (old, old))

    removed = timelapse.prune_old_timelapses()
    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_prune_old_handles_missing_root(data_dir, tmp_path):
    from app import timelapse
    _setup_storage(data_dir, None, storage_path=str(tmp_path / "ghost"))
    # No-op rather than an exception.
    assert timelapse.prune_old_timelapses() == 0


def test_buildresult_serializable(data_dir):
    from app.timelapse import BuildResult
    r = BuildResult(camera="cam1", date="2026-04-28", output="/x.mp4",
                    frame_count=1440)
    d = r.to_dict()
    assert d["camera"] == "cam1"
    assert d["frame_count"] == 1440
    assert d["error"] == ""


# ---- routes ------------------------------------------------------------

def test_timelapse_list_route(authed_client, tmp_path):
    """The list route should return the items returned by list_timelapses."""
    from app import timelapse, config_store
    cfg = config_store.load_config()
    cfg.storage_path = str(tmp_path / "store")
    config_store.save_config(cfg)
    root = tmp_path / "store" / "timelapse"
    root.mkdir(parents=True)
    (root / "cam1-2026-04-28.mp4").write_bytes(b"hello")
    resp = authed_client.get("/api/timelapse")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(i["filename"] == "cam1-2026-04-28.mp4" for i in items)


def test_timelapse_download_rejects_traversal(authed_client):
    """A path-traversal filename must NOT escape the storage root."""
    resp = authed_client.get("/api/timelapse/..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_timelapse_build_now_requires_admin(app_client, data_dir):
    """An unauthenticated client cannot trigger a build."""
    resp = app_client.post("/api/timelapse/build-now")
    assert resp.status_code in (401, 403)
