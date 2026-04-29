"""Per-camera daily time-lapse.

Pipeline:
  1. Background sampler runs every TIMELAPSE_FRAME_INTERVAL_SECONDS
     (default 60 s = 1 frame/min). For each camera it pulls a fresh
     latest.jpg from Frigate and saves to
     {STORAGE_PATH}/timelapse/{date}/{camera}/{HHMMSS}.jpg
  2. Once per day at TIMELAPSE_HOUR (default 02:30 — after the
     highlights run), the builder takes yesterday's frames per
     camera and ffmpegs them into one mp4 at
     {STORAGE_PATH}/timelapse/{camera}-{date}.mp4
  3. Source frames are then deleted (they're heavy — 1440 jpegs
     × ~80 KB ≈ 115 MB per camera per day — but the resulting mp4
     is ~5 MB).

ffmpeg invocation: image2 demuxer + libx264-friendly fallback.
We DO encode here (no `-c copy` shortcut because we're going
jpeg → mp4) but ffmpeg uses libx264 which is GPL-licensed via
Debian's gpl-build. License-wise this is the same situation as
Frigate: subprocess invocation, no library linking. NOTICES.md
already calls this out.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import cameras_store, config_store

logger = logging.getLogger("pawcorder.timelapse")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "http://frigate:5000")

TIMELAPSE_FRAME_INTERVAL_SECONDS = int(os.environ.get("PAWCORDER_TIMELAPSE_INTERVAL", "60"))
TIMELAPSE_BUILD_HOUR = int(os.environ.get("PAWCORDER_TIMELAPSE_HOUR", "2"))
TIMELAPSE_FPS = 30  # 24h of 1/min frames at 30 fps = 48s reel — easy to share
RETENTION_DAYS = 30


def storage_root() -> Path:
    cfg = config_store.load_config()
    return Path(cfg.storage_path or "/mnt/pawcorder") / "timelapse"


def frames_dir(date_label: str, camera: str) -> Path:
    return storage_root() / "frames" / date_label / camera


def output_path(date_label: str, camera: str) -> Path:
    return storage_root() / f"{camera}-{date_label}.mp4"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# ---- frame sampling ----------------------------------------------------

async def _grab_frame(camera: str) -> bytes | None:
    url = f"{FRIGATE_BASE_URL}/api/{camera}/latest.jpg"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.content:
            return None
        return resp.content
    except httpx.HTTPError:
        return None


async def sample_all_cameras() -> int:
    """One pass: grab a fresh frame for every enabled camera and save
    to disk. Returns the count of frames saved."""
    cams = [c for c in cameras_store.CameraStore().load() if c.enabled]
    if not cams:
        return 0
    date_label = time.strftime("%Y-%m-%d", time.localtime())
    stamp = time.strftime("%H%M%S", time.localtime())
    saved = 0
    for cam in cams:
        bytes_ = await _grab_frame(cam.name)
        if not bytes_:
            continue
        target_dir = frames_dir(date_label, cam.name)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            (target_dir / f"{stamp}.jpg").write_bytes(bytes_)
            saved += 1
        except OSError as exc:
            logger.warning("could not save timelapse frame: %s", exc)
    return saved


# ---- builder -----------------------------------------------------------

@dataclass
class BuildResult:
    camera: str
    date: str
    output: str | None
    frame_count: int
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "camera": self.camera, "date": self.date,
            "output": self.output, "frame_count": self.frame_count,
            "error": self.error,
        }


def build_one(camera: str, date_label: str) -> BuildResult:
    """ffmpeg image2 demuxer + libx264 → mp4. Frames are deleted after
    a successful build to free up space."""
    src_dir = frames_dir(date_label, camera)
    out_path = output_path(date_label, camera)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        return BuildResult(camera=camera, date=date_label,
                           output=str(out_path), frame_count=0)
    if not src_dir.exists():
        return BuildResult(camera=camera, date=date_label, output=None,
                           frame_count=0, error="no frames captured")

    frames = sorted(src_dir.glob("*.jpg"))
    if len(frames) < 10:
        return BuildResult(camera=camera, date=date_label, output=None,
                           frame_count=len(frames),
                           error=f"too few frames ({len(frames)})")

    if not ffmpeg_available():
        return BuildResult(camera=camera, date=date_label, output=None,
                           frame_count=len(frames),
                           error="ffmpeg not on PATH")

    # The image2 demuxer wants a glob pattern. Frames are HHMMSS-sorted
    # so a plain `cat` works.
    list_file = src_dir / ".concat.txt"
    list_file.write_text(
        "\n".join(f"file '{f.resolve()}'\nduration {1.0 / TIMELAPSE_FPS}" for f in frames)
        + f"\nfile '{frames[-1].resolve()}'\n",  # last frame held to end
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-vsync", "vfr",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        list_file.unlink(missing_ok=True)
        return BuildResult(camera=camera, date=date_label, output=None,
                           frame_count=len(frames), error=f"ffmpeg error: {exc}")
    list_file.unlink(missing_ok=True)
    if proc.returncode != 0:
        return BuildResult(camera=camera, date=date_label, output=None,
                           frame_count=len(frames),
                           error=(proc.stderr or b"")[-200:].decode("utf-8", errors="replace"))

    # Success — wipe the source jpegs.
    shutil.rmtree(src_dir, ignore_errors=True)
    return BuildResult(camera=camera, date=date_label,
                       output=str(out_path), frame_count=len(frames))


def build_yesterday() -> list[BuildResult]:
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    cams = cameras_store.CameraStore().load()
    return [build_one(c.name, yesterday) for c in cams]


# ---- listing + retention ----------------------------------------------

def list_timelapses() -> list[dict]:
    """Newest first, for the /timelapse UI."""
    root = storage_root()
    if not root.exists():
        return []
    rows: list[dict] = []
    for p in sorted(root.glob("*.mp4"), reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        # Filename: <camera>-<YYYY-MM-DD>.mp4 — split on the last "-YYYY-MM-DD"
        # suffix, NOT just the last "-", because the date itself contains
        # hyphens (so rpartition would yank the wrong field).
        name = p.stem
        import re as _re
        m = _re.match(r"^(.*)-(\d{4}-\d{2}-\d{2})$", name)
        if m:
            cam, date = m.group(1), m.group(2)
        else:
            cam, date = name, ""
        rows.append({
            "filename": p.name, "camera": cam, "date": date,
            "size_bytes": st.st_size, "modified": int(st.st_mtime),
        })
    return rows


def prune_old_timelapses() -> int:
    """Drop mp4s older than RETENTION_DAYS. Frame dirs are deleted
    on successful build, so they're not part of this pass."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    root = storage_root()
    if not root.exists():
        return 0
    for p in root.glob("*.mp4"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---- background scheduler --------------------------------------------

class TimelapseScheduler:
    """One asyncio task that does TWO things:
      - every TIMELAPSE_FRAME_INTERVAL_SECONDS, sample a frame.
      - once per day past TIMELAPSE_BUILD_HOUR, build yesterday's reel.
    """
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_built_date: str = ""

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="timelapse-scheduler")
            logger.info("timelapse scheduler started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await sample_all_cameras()
                self._maybe_build()
            except Exception as exc:  # noqa: BLE001
                logger.warning("timelapse tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=TIMELAPSE_FRAME_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

    def _maybe_build(self) -> None:
        now = time.localtime()
        if now.tm_hour < TIMELAPSE_BUILD_HOUR:
            return
        date_label = time.strftime("%Y-%m-%d", now)
        if self._last_built_date == date_label:
            return
        # Stamp BEFORE building so a concurrent tick (we're called from a
        # single coroutine but a future refactor could parallelize) doesn't
        # double-fire. build_one is idempotent (output_path exists check)
        # so a re-entry would be a no-op anyway, but belt + suspenders.
        self._last_built_date = date_label
        results = build_yesterday()
        for r in results:
            if r.output:
                logger.info("timelapse built %s/%s", r.camera, r.date)
            else:
                logger.info("timelapse skipped %s/%s: %s", r.camera, r.date, r.error)
        prune_old_timelapses()


scheduler = TimelapseScheduler()
