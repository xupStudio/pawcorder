"""Tests for daily highlights reel."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_ffmpeg_available_runs(data_dir):
    """Just confirm we can call the helper without it crashing."""
    from app import highlights
    # We can't assert True/False — depends on the test environment.
    assert isinstance(highlights.ffmpeg_available(), bool)


def test_output_dir_uses_storage_path(data_dir):
    from app import config_store, highlights

    cfg = config_store.load_config()
    cfg.storage_path = str(data_dir / "fake-storage")
    config_store.save_config(cfg)

    out = highlights.output_dir()
    assert out == data_dir / "fake-storage" / "highlights"


def test_list_highlights_empty_when_dir_missing(data_dir):
    from app import highlights
    assert highlights.list_highlights() == []


def test_list_highlights_sorted_newest_first(data_dir):
    from app import config_store, highlights

    cfg = config_store.load_config()
    cfg.storage_path = str(data_dir / "fake-storage")
    config_store.save_config(cfg)

    out_dir = highlights.output_dir()
    out_dir.mkdir(parents=True)
    (out_dir / "2026-04-26.mp4").write_bytes(b"a")
    (out_dir / "2026-04-28.mp4").write_bytes(b"b")
    (out_dir / "2026-04-27.mp4").write_bytes(b"c")

    rows = highlights.list_highlights()
    assert [r["date"] for r in rows] == ["2026-04-28", "2026-04-27", "2026-04-26"]


def test_build_no_ffmpeg_returns_error(data_dir, monkeypatch):
    """No ffmpeg on PATH → result.error explains why, no crash."""
    import asyncio
    from app import highlights

    monkeypatch.setattr(highlights, "ffmpeg_available", lambda: False)
    result = asyncio.run(highlights.build_highlights_for(0, 86400))
    assert result.output_path is None
    assert "ffmpeg" in result.error


def test_build_no_events_returns_error(data_dir, monkeypatch):
    """ffmpeg present, but Frigate returned 0 events → soft skip."""
    import asyncio
    from app import highlights

    monkeypatch.setattr(highlights, "ffmpeg_available", lambda: True)
    async def _no_events(*a, **k): return []
    monkeypatch.setattr(highlights, "_fetch_top_events", _no_events)

    result = asyncio.run(highlights.build_highlights_for(0, 86400))
    assert result.output_path is None
    assert "no events" in result.error


def test_build_idempotent_when_output_exists(data_dir, monkeypatch):
    """If today's mp4 is already on disk, we don't redo the work.

    We pass the SAME timestamps the existing file is named for so the
    build's date_label matches; otherwise the function would build a
    fresh reel for a different date.
    """
    import asyncio
    import time
    from app import config_store, highlights

    cfg = config_store.load_config()
    cfg.storage_path = str(data_dir / "fake-storage")
    config_store.save_config(cfg)

    monkeypatch.setattr(highlights, "ffmpeg_available", lambda: True)
    out_dir = highlights.output_dir()
    out_dir.mkdir(parents=True)
    # Pin to a known timestamp so the file we write and the build call
    # share the same date label.
    pinned_start = time.time() - 100
    date_label = time.strftime("%Y-%m-%d", time.localtime(pinned_start))
    existing = out_dir / f"{date_label}.mp4"
    existing.write_bytes(b"already there")

    fake_called = []
    async def _ev(*a, **k):
        fake_called.append(1)
        return [{"id": "e1", "label": "cat", "has_clip": True, "top_score": 0.9}]
    monkeypatch.setattr(highlights, "_fetch_top_events", _ev)

    result = asyncio.run(highlights.build_highlights_for(pinned_start, pinned_start + 100))
    assert result.output_path == str(existing)


# ---- routes ------------------------------------------------------------

def test_list_route_returns_payload(authed_client):
    resp = authed_client.get("/api/highlights")
    assert resp.status_code == 200
    assert "highlights" in resp.json()


def test_download_rejects_invalid_filename(authed_client):
    """Filename must match YYYY-MM-DD.mp4 — anything else 400."""
    resp = authed_client.get("/api/highlights/../etc.mp4")
    # FastAPI rejects the URL because of the slash; either 400 or 404 is OK
    assert resp.status_code in (400, 404)
    resp = authed_client.get("/api/highlights/random.mp4")
    assert resp.status_code == 400


def test_build_now_503_if_no_ffmpeg(authed_client, monkeypatch):
    from app import highlights
    monkeypatch.setattr(highlights, "ffmpeg_available", lambda: False)
    resp = authed_client.post("/api/highlights/build-now", json={})
    assert resp.status_code == 503
