"""Activity heatmap per camera.

Frigate stores each event's bounding-box trail as a list of
[x, y, w, h] frames in the camera's resolution. Over a 30-day
window, accumulating those bbox centers into a 2D histogram and
overlaying the result on a still frame produces "where the cat hangs
out" — a visual the user can show off.

The heatmap is rendered server-side as a translucent PNG that the UI
overlays on the camera's `latest.jpg`. We keep it cached on disk so
30 days of pulls + image generation only happen on demand.

Implementation notes:
  - Uses Pillow (already a dep) for the actual pixel work.
  - Numpy for the histogram bin math.
  - Resolution is fixed 64×36 (16:9) buckets — coarse enough that
    one curl-up has visible weight, fine enough that detail isn't
    lost. Smoothed via box blur before colormapping.
  - We DON'T re-pull events on every render — there's a one-hour
    cache per camera. Refresh button forces re-pull.
"""
from __future__ import annotations

import io
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import numpy as np

logger = logging.getLogger("pawcorder.heatmap")

FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "http://frigate:5000")
DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
CACHE_DIR = DATA_DIR / "config" / "heatmaps"

# 64 × 36 buckets covers a 16:9 frame at "blob detail, not pixel art".
GRID_W, GRID_H = 64, 36
DEFAULT_LOOKBACK_DAYS = 30
CACHE_TTL_SECONDS = 60 * 60  # one hour


@dataclass
class HeatmapResult:
    camera: str
    grid: np.ndarray         # shape (GRID_H, GRID_W), float
    sample_count: int        # number of bbox centers used
    generated_at: float      # unix seconds


# ---- event fetching ----------------------------------------------------

async def _fetch_events_with_data(camera: str, *, since: float, limit: int = 5000) -> list[dict]:
    """Pull recent events that contain bbox data. Frigate's events API
    has a `data.box` or `top_score`-only depending on version; we
    extract whatever we can find."""
    url = f"{FRIGATE_BASE_URL}/api/events"
    params = {
        "after": int(since),
        "camera": camera,
        "limit": limit,
        "include_thumbnails": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        return resp.json() or []
    except (httpx.HTTPError, ValueError):
        return []


def _bbox_centers(events: list[dict]) -> list[tuple[float, float]]:
    """Extract a list of (x, y) center points in 0..1 coords.

    Each Frigate event has `box` (final box) or `region` (last region).
    We use whatever is present. Coordinates in Frigate are 0..1 already
    since the detection grid is fixed.
    """
    out: list[tuple[float, float]] = []
    for e in events:
        box = e.get("box") or e.get("region")
        if not box or len(box) < 4:
            # Newer Frigate sometimes nests under "data".
            data = e.get("data") or {}
            box = data.get("box") or data.get("region")
        if not box or len(box) < 4:
            continue
        x, y, w, h = box[0], box[1], box[2], box[3]
        # Some versions store px coords (>1), others 0..1. If max > 1
        # we assume px and need a normaliser — but without the frame
        # size we can't normalise. Skip these events to be safe.
        if max(x, y, x + w, y + h) > 1.5:
            continue
        cx = float(x) + float(w) / 2
        cy = float(y) + float(h) / 2
        if 0 <= cx <= 1 and 0 <= cy <= 1:
            out.append((cx, cy))
    return out


# ---- histogram + render ------------------------------------------------

def _accumulate(centers: list[tuple[float, float]]) -> np.ndarray:
    """Centers → (GRID_H, GRID_W) float histogram, smoothed."""
    grid = np.zeros((GRID_H, GRID_W), dtype=np.float32)
    for cx, cy in centers:
        col = min(int(cx * GRID_W), GRID_W - 1)
        row = min(int(cy * GRID_H), GRID_H - 1)
        grid[row, col] += 1.0
    # Box blur — averages each cell with its 8 neighbours so a single
    # cluster doesn't render as a sharp single pixel.
    smoothed = np.zeros_like(grid)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            shifted = np.roll(grid, (dy, dx), axis=(0, 1))
            smoothed += shifted
    return smoothed / 9.0


def _colormap(value: float) -> tuple[int, int, int, int]:
    """Inferno-ish gradient: 0 → transparent black, 1 → opaque red.

    Compact 5-stop gradient; good enough that we don't ship matplotlib.
    Returns (r, g, b, a) bytes.
    """
    if value <= 0:
        return (0, 0, 0, 0)
    v = max(0.0, min(1.0, value))
    # Stops: dark blue → magenta → orange → yellow → white
    stops = [
        (0.0, (0, 0, 60, 0)),
        (0.2, (60, 10, 100, 90)),
        (0.45, (180, 30, 80, 160)),
        (0.7, (240, 130, 40, 200)),
        (1.0, (255, 240, 180, 220)),
    ]
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if v <= t1:
            frac = (v - t0) / (t1 - t0) if t1 > t0 else 0
            r = int(c0[0] + (c1[0] - c0[0]) * frac)
            g = int(c0[1] + (c1[1] - c0[1]) * frac)
            b = int(c0[2] + (c1[2] - c0[2]) * frac)
            a = int(c0[3] + (c1[3] - c0[3]) * frac)
            return (r, g, b, a)
    return stops[-1][1]


def render_heatmap_png(grid: np.ndarray, *, output_w: int = 1280, output_h: int = 720) -> bytes:
    """Render the histogram to a translucent PNG ready to overlay on
    `latest.jpg`. Output is full-camera-resolution to match the
    underlying frame the UI will composite it over.
    """
    from PIL import Image  # local — only the heatmap path needs Pillow loaded

    # Normalise to 0..1 — the brightest cell becomes "max" colour.
    peak = grid.max()
    if peak <= 0:
        # Empty heatmap → fully-transparent png.
        img = Image.new("RGBA", (output_w, output_h), (0, 0, 0, 0))
    else:
        normed = grid / peak
        # Build the small RGBA image at GRID_W × GRID_H, then resize up.
        rgba = np.zeros((GRID_H, GRID_W, 4), dtype=np.uint8)
        for row in range(GRID_H):
            for col in range(GRID_W):
                rgba[row, col] = _colormap(float(normed[row, col]))
        img = Image.fromarray(rgba, mode="RGBA")
        img = img.resize((output_w, output_h), Image.BILINEAR)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---- end-to-end helper -------------------------------------------------

async def build_heatmap(camera: str, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                        force: bool = False) -> Optional[HeatmapResult]:
    """Load events, accumulate, return the grid. Caches the rendered
    PNG on disk under config/heatmaps/<camera>.png + <camera>.json
    (metadata)."""
    since = time.time() - lookback_days * 86400
    events = await _fetch_events_with_data(camera, since=since)
    centers = _bbox_centers(events)
    grid = _accumulate(centers)
    return HeatmapResult(
        camera=camera, grid=grid, sample_count=len(centers),
        generated_at=time.time(),
    )


def cached_png_path(camera: str) -> Path:
    return CACHE_DIR / f"{camera}.png"


def cache_metadata_path(camera: str) -> Path:
    return CACHE_DIR / f"{camera}.meta.json"


async def get_or_build_png(camera: str, *, force: bool = False) -> tuple[bytes, dict]:
    """Return the PNG bytes + metadata. Uses the disk cache when
    fresh."""
    import json
    png_path = cached_png_path(camera)
    meta_path = cache_metadata_path(camera)

    if not force and png_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if time.time() - float(meta.get("generated_at") or 0) < CACHE_TTL_SECONDS:
                return png_path.read_bytes(), meta
        except (OSError, ValueError):
            pass

    result = await build_heatmap(camera, force=force)
    if result is None:
        png = render_heatmap_png(np.zeros((GRID_H, GRID_W), dtype=np.float32))
        meta = {"sample_count": 0, "generated_at": time.time(), "camera": camera}
    else:
        png = render_heatmap_png(result.grid)
        meta = {
            "sample_count": result.sample_count,
            "generated_at": result.generated_at,
            "camera": camera,
        }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(png)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return png, meta
