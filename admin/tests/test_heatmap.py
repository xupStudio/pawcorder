"""Tests for activity heatmap rendering."""
from __future__ import annotations

import numpy as np
import pytest


def test_bbox_centers_0_to_1(data_dir):
    from app.heatmap import _bbox_centers
    events = [
        {"box": [0.1, 0.2, 0.4, 0.4]},
        {"region": [0.0, 0.0, 1.0, 1.0]},
    ]
    centers = _bbox_centers(events)
    assert len(centers) == 2
    cx, cy = centers[0]
    assert pytest.approx(cx, abs=0.01) == 0.3  # 0.1 + 0.4/2
    assert pytest.approx(cy, abs=0.01) == 0.4


def test_bbox_centers_skips_pixel_coords(data_dir):
    """Some Frigate versions return px coords (>1). We can't normalise
    without the frame size, so skip rather than corrupt the heatmap."""
    from app.heatmap import _bbox_centers
    events = [{"box": [0, 0, 1280, 720]}]
    assert _bbox_centers(events) == []


def test_bbox_centers_handles_missing(data_dir):
    from app.heatmap import _bbox_centers
    assert _bbox_centers([{}, {"label": "cat"}]) == []


def test_accumulate_produces_smoothed_grid(data_dir):
    from app.heatmap import _accumulate, GRID_W, GRID_H
    grid = _accumulate([(0.5, 0.5), (0.5, 0.5)])
    assert grid.shape == (GRID_H, GRID_W)
    # The centre cell should be hottest after the 3×3 box blur — but
    # because of the smoothing, max value is 2/9 not 2.
    assert grid[GRID_H // 2, GRID_W // 2] > 0


def test_render_empty_heatmap_is_transparent(data_dir):
    """No data → fully transparent PNG, which the UI will composite to
    "no overlay" without ugly artifacts."""
    from app.heatmap import _accumulate, render_heatmap_png
    grid = _accumulate([])
    png = render_heatmap_png(grid, output_w=100, output_h=80)
    # Open it back and check pixel values.
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # Sample several pixels — all should have alpha=0.
    for x, y in [(0, 0), (50, 40), (99, 79)]:
        _, _, _, a = img.getpixel((x, y))
        assert a == 0


def test_render_with_data_has_visible_pixels(data_dir):
    from app.heatmap import _accumulate, render_heatmap_png
    grid = _accumulate([(0.5, 0.5)] * 5)  # 5 hits at the center
    png = render_heatmap_png(grid, output_w=100, output_h=80)
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # Centre region should have some non-zero alpha.
    _, _, _, a = img.getpixel((50, 40))
    assert a > 0


def test_get_or_build_caches_to_disk(data_dir, monkeypatch):
    import asyncio
    from app import heatmap

    async def _fake_events(*a, **k):
        return [{"box": [0.4, 0.4, 0.2, 0.2]}]
    monkeypatch.setattr(heatmap, "_fetch_events_with_data", _fake_events)

    png, meta = asyncio.run(heatmap.get_or_build_png("kitchen"))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    assert meta["sample_count"] == 1
    assert heatmap.cached_png_path("kitchen").exists()


def test_get_or_build_uses_cache_within_ttl(data_dir, monkeypatch):
    import asyncio
    from app import heatmap

    call_count = []
    async def _fake_events(*a, **k):
        call_count.append(1)
        return [{"box": [0.4, 0.4, 0.2, 0.2]}]
    monkeypatch.setattr(heatmap, "_fetch_events_with_data", _fake_events)

    asyncio.run(heatmap.get_or_build_png("kitchen"))
    asyncio.run(heatmap.get_or_build_png("kitchen"))
    # Second call within TTL: did NOT re-fetch events.
    assert len(call_count) == 1


# ---- route -------------------------------------------------------------

def test_heatmap_route_returns_png(authed_client, monkeypatch):
    from app import heatmap
    async def _fake_events(*a, **k): return [{"box": [0.5, 0.5, 0.1, 0.1]}]
    monkeypatch.setattr(heatmap, "_fetch_events_with_data", _fake_events)

    authed_client.post("/api/cameras", json={"name": "kitchen", "ip": "1.1.1.1", "password": "p"})
    resp = authed_client.get("/api/cameras/kitchen/heatmap")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers.get("X-Heatmap-Samples") == "1"


def test_heatmap_route_404_unknown_camera(authed_client):
    resp = authed_client.get("/api/cameras/ghost/heatmap")
    assert resp.status_code == 404
