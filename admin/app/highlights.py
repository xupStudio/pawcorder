"""Daily highlights reel — auto-edit the day's best events into a
30-60s mp4 the user can share.

Pipeline:
  1. Once per day at HIGHLIGHTS_HOUR (local time), the background task
     runs.
  2. Pull all events from Frigate's /api/events for the past 24h.
  3. Rank by top_score; take TOP_N.
  4. For each, ask Frigate for the clip URL (clip.mp4).
  5. ffmpeg `-c copy` to trim to 5-8 seconds, then concat.
  6. Save the result under {STORAGE_PATH}/highlights/YYYY-MM-DD.mp4
  7. (Optional) push to Telegram.

Why ffmpeg `-c copy` and not re-encode: stream copy doesn't decode or
encode any frames, so it carries no patent obligation beyond what your
camera/host already has. Trade-off is that cuts can only land on
keyframes (every 2-5 s in normal Frigate output), so the reel won't
have frame-perfect transitions — that's fine for a highlights mash-up.

Soft-fails everywhere: if ffmpeg's missing, Frigate's down, or there
were no events today, we just skip and try again tomorrow. Never
crash the background loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import config_store

logger = logging.getLogger("pawcorder.highlights")

FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "http://frigate:5000")
DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))

# When during the day to run. 02:00 by default — quiet for most homes,
# avoids competing with peak Frigate inference load.
HIGHLIGHTS_HOUR = int(os.environ.get("PAWCORDER_HIGHLIGHTS_HOUR", "2"))
TOP_N = 5
PER_CLIP_SECONDS = 6
LOOP_CHECK_INTERVAL_SECONDS = 600  # 10 min — coarse polling is fine
RETENTION_DAYS = 14                # delete reels older than this


@dataclass
class HighlightResult:
    """One day's worth of highlight generation — for the /highlights UI."""
    date: str                      # YYYY-MM-DD
    output_path: Optional[str] = None
    duration_seconds: int = 0
    events_used: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "output_path": self.output_path,
            "duration_seconds": self.duration_seconds,
            "events_used": list(self.events_used),
            "error": self.error,
        }


def output_dir() -> Path:
    """Highlights live alongside Frigate recordings — same drive that
    has space for big mp4s."""
    cfg = config_store.load_config()
    return Path(cfg.storage_path or "/mnt/pawcorder") / "highlights"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# ---- event fetching ----------------------------------------------------

async def _fetch_top_events(after_ts: float, before_ts: float, *,
                             labels: tuple[str, ...] = ("cat", "dog"),
                             limit: int = 25) -> list[dict]:
    """Pull events from Frigate, filter to pet labels with clips."""
    url = f"{FRIGATE_BASE_URL}/api/events"
    params = {
        "after": int(after_ts),
        "before": int(before_ts),
        "has_clip": 1,
        "limit": limit,
        "include_thumbnails": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        rows = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    keep = [e for e in rows if e.get("label") in labels and e.get("has_clip")]
    keep.sort(key=lambda e: float(e.get("top_score") or e.get("score") or 0), reverse=True)
    return keep[:TOP_N]


async def _download_clip(event_id: str, dest: Path) -> bool:
    url = f"{FRIGATE_BASE_URL}/api/events/{event_id}/clip.mp4"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
        return dest.exists() and dest.stat().st_size > 0
    except httpx.HTTPError:
        return False


# ---- ffmpeg work (sync, run in executor) -------------------------------

def _trim_clip(src: Path, dst: Path, *, max_seconds: int) -> bool:
    """ffmpeg -c copy trims to roughly max_seconds, keyframe-aligned.

    `-ss 0 -t N -c copy` cuts at the first keyframe ≥ N seconds. We
    don't try to be precise — the result is a small mp4 that the
    concat below can stream-copy cleanly.
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-t", str(max_seconds),
        "-c", "copy",
        "-movflags", "+faststart",  # web-friendly, makes Telegram preview work
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        if proc.returncode != 0:
            logger.warning("ffmpeg trim failed for %s: %s", src.name,
                           proc.stderr[-200:].decode("utf-8", errors="replace"))
            return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ffmpeg trim error: %s", exc)
        return False
    return dst.exists() and dst.stat().st_size > 0


def _concat_clips(clips: list[Path], dst: Path) -> bool:
    """ffmpeg concat demuxer — stream-copy all inputs into one mp4."""
    if not clips:
        return False
    list_file = dst.with_suffix(".concat.txt")
    list_file.write_text(
        "\n".join(f"file '{c.resolve()}'" for c in clips) + "\n",
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if proc.returncode != 0:
            logger.warning("ffmpeg concat failed: %s",
                           proc.stderr[-300:].decode("utf-8", errors="replace"))
            return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ffmpeg concat error: %s", exc)
        return False
    finally:
        list_file.unlink(missing_ok=True)
    return dst.exists() and dst.stat().st_size > 0


# ---- one day's run -----------------------------------------------------

async def build_highlights_for(day_start: float, day_end: float) -> HighlightResult:
    """Materialise one day's highlight reel.

    `day_start` / `day_end` are unix timestamps. Default callers pass
    [yesterday 00:00, yesterday 23:59:59] in local time.
    """
    date_label = time.strftime("%Y-%m-%d", time.localtime(day_start))
    result = HighlightResult(date=date_label)

    if not ffmpeg_available():
        result.error = "ffmpeg not on PATH"
        return result

    events = await _fetch_top_events(day_start, day_end)
    if not events:
        result.error = "no events today"
        return result

    out_dir = output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / f"{date_label}.mp4"
    if final_path.exists():
        # Already built today — idempotent.
        result.output_path = str(final_path)
        result.events_used = ["(existing)"]
        return result

    tmp_dir = Path("/tmp/pawcorder-highlights") / date_label
    tmp_dir.mkdir(parents=True, exist_ok=True)
    trimmed: list[Path] = []
    try:
        for i, event in enumerate(events):
            event_id = event.get("id")
            if not event_id:
                continue
            raw = tmp_dir / f"raw_{i:02d}.mp4"
            if not await _download_clip(str(event_id), raw):
                continue
            trimmed_clip = tmp_dir / f"clip_{i:02d}.mp4"
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(
                None, _trim_clip, raw, trimmed_clip, PER_CLIP_SECONDS,
            )
            if ok:
                trimmed.append(trimmed_clip)
                result.events_used.append(str(event_id))
            raw.unlink(missing_ok=True)

        if not trimmed:
            result.error = "no clips usable after trim"
            return result

        loop = asyncio.get_event_loop()
        if not await loop.run_in_executor(None, _concat_clips, trimmed, final_path):
            result.error = "concat failed"
            return result

        result.output_path = str(final_path)
        result.duration_seconds = PER_CLIP_SECONDS * len(trimmed)
    finally:
        # Always clean tmp_dir — it can hold ~10-50 MB of intermediate clips.
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return result


async def maybe_push_to_telegram(result: HighlightResult) -> None:
    """If Telegram is enabled, send the reel as a video. Soft-fail."""
    if not result.output_path:
        return
    cfg = config_store.load_config()
    if not (cfg.telegram_enabled and cfg.telegram_bot_token and cfg.telegram_chat_id):
        return

    path = Path(result.output_path)
    if not path.exists():
        return
    # Telegram bot has a 50 MB upload cap; if our reel is bigger we
    # just send a text pointer rather than fail.
    if path.stat().st_size > 49 * 1024 * 1024:
        text = (f"<b>pawcorder daily highlights — {result.date}</b>\n"
                f"{len(result.events_used)} events, {result.duration_seconds}s. "
                f"File too large for Telegram; saved at {result.output_path}")
        try:
            from .telegram import send_message
            await send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        from .telegram import _api_call  # internal — already does the auth dance
        with path.open("rb") as f:
            await _api_call(
                cfg.telegram_bot_token, "sendVideo",
                data={
                    "chat_id": cfg.telegram_chat_id,
                    "caption": f"<b>pawcorder daily highlights — {result.date}</b>\n"
                               f"{len(result.events_used)} events, {result.duration_seconds}s",
                    "parse_mode": "HTML",
                },
                files={"video": (path.name, f, "video/mp4")},
                timeout=120.0,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram highlight send failed: %s", exc)


# ---- background scheduler ----------------------------------------------

class HighlightsScheduler:
    """Wakes up every LOOP_CHECK_INTERVAL_SECONDS, checks if today's
    HIGHLIGHTS_HOUR already passed, and if so runs the daily build for
    the day that just ended.

    Idempotent on disk: if the output mp4 for that date exists, skip.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_built_date: str = ""

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="highlights-scheduler")
            logger.info("highlights scheduler started")

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
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("highlights tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=LOOP_CHECK_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        """Run a build if we just crossed HIGHLIGHTS_HOUR and we haven't
        built today's reel yet."""
        now = time.localtime()
        if now.tm_hour < HIGHLIGHTS_HOUR:
            return
        # The reel covers YESTERDAY (the day that just ended).
        # Compute yesterday's [start, end] in local time.
        today_midnight = time.mktime(time.struct_time(
            (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, now.tm_isdst)
        ))
        yesterday_start = today_midnight - 86400
        yesterday_end = today_midnight - 1
        date_label = time.strftime("%Y-%m-%d", time.localtime(yesterday_start))
        if self._last_built_date == date_label:
            return
        result = await build_highlights_for(yesterday_start, yesterday_end)
        self._last_built_date = date_label
        if result.output_path:
            logger.info("highlights built for %s: %s", date_label, result.output_path)
            await maybe_push_to_telegram(result)
        else:
            logger.info("highlights skipped for %s: %s", date_label, result.error)
        # Always run retention so the dir doesn't grow forever even if
        # individual builds fail.
        try:
            pruned = prune_old_highlights()
            if pruned:
                logger.info("highlights retention pruned %d old reels", pruned)
        except Exception as exc:  # noqa: BLE001
            logger.warning("highlights prune failed: %s", exc)


scheduler = HighlightsScheduler()


# ---- discovery for /highlights UI -------------------------------------

def list_highlights() -> list[dict]:
    """All highlight reels currently on disk, newest first."""
    out_dir = output_dir()
    if not out_dir.exists():
        return []
    rows: list[dict] = []
    for p in sorted(out_dir.glob("*.mp4"), reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        rows.append({
            "date": p.stem,
            "filename": p.name,
            "size_bytes": st.st_size,
            "modified": int(st.st_mtime),
        })
    return rows


def prune_old_highlights() -> int:
    """Delete reels older than RETENTION_DAYS. Run after each daily
    build so the dir doesn't grow forever (1 reel/day × 14 days × ~5
    MB ≈ 70 MB cap). Returns count removed."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    out_dir = output_dir()
    if not out_dir.exists():
        return 0
    for p in out_dir.glob("*.mp4"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed
