"""Small cross-cutting helpers that don't belong to any one module.

Everything here is intentionally dependency-free (stdlib only) so it
can be imported from anywhere — config_store, privacy, cameras_store,
backup — without creating circular imports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from fastapi import UploadFile


def read_ndjson(path: Path, *,
                 filter_fn: Optional[Callable[[dict], bool]] = None,
                 sort_key: Optional[Callable[[dict], object]] = None,
                 reverse: bool = False,
                 limit: Optional[int] = None) -> list[dict]:
    """Stream-decode an NDJSON file, skipping malformed lines.

    Used by the sightings log, the diary log, and any future ND-line
    state — all of them share the same shape: scan, json.loads each
    line, apply an optional filter, optionally sort + cap, return a
    list of dicts. Centralising it here keeps each call site to one
    line and makes "what happens to a corrupt mid-line on disk?"
    a single answer instead of three.

    Returns [] if the file doesn't exist or can't be read — callers
    treat both as "no data" rather than as an error.
    """
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if filter_fn is not None and not filter_fn(row):
                    continue
                out.append(row)
    except OSError:
        return []
    if sort_key is not None:
        out.sort(key=sort_key, reverse=reverse)
    if limit is not None:
        out = out[:limit]
    return out


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Atomic UTF-8 write: temp sibling + os.replace.

    Why we keep reaching for this: any code path that owns a critical
    on-disk file (.env, cameras.yml, config.yml, privacy.json) must
    survive being killed mid-write. Plain Path.write_text truncates the
    target first — a kill between truncate and re-fill leaves a half-
    written file that the loader silently reads as empty/default,
    silently dropping the user's data.

    The .writing temp lives in the same directory so os.replace is a
    rename within a filesystem (atomic on POSIX, near-atomic on Windows
    via MoveFileEx). chmod is best-effort — bind-mounted volumes from
    macOS Docker hosts can refuse it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".writing")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except (PermissionError, OSError):
        # PermissionError on shared volumes; OSError on read-only fs.
        # Either way the temp's content is correct, just keep going.
        pass
    os.replace(tmp, path)


class UploadTooLarge(Exception):
    """Raised by read_capped_upload when the input exceeds max_bytes."""


class PollingTask:
    """Cancellable async loop with start/stop lifecycle.

    Pattern that re-occurred 4× in the recent feature drop (diary
    scheduler, pet-health monitor, litter monitor, fight detector) —
    each had its own copy of "create_task → loop → wait_for(stop_event,
    timeout=interval) → swallow per-tick exceptions". Centralising
    those mechanics here lets each detector be just a `name`,
    `interval_seconds`, and a `_tick()` implementation.

    Migration status: the four feature-drop pollers above have been
    converted. Seven older OSS pollers (``health.HealthMonitor``,
    ``cloud.CloudUploader``, ``timelapse.TimelapseScheduler``,
    ``highlights.HighlightsScheduler``, ``telegram.poller``,
    ``backup_schedule.BackupScheduler``, ``privacy.monitor``) keep
    their bespoke lifecycles for now — the conversion is a separate,
    risk-isolated PR.

    Subclass override:
        async def _tick(self) -> None: ...

    Or pass a callable via tick_fn= for one-off pollers without a class.

    Public surface mirrors the existing duplicates, so swapping a
    bespoke poller in is a delete-and-extend.
    """

    name: str = "polling-task"
    interval_seconds: float = 60.0

    def __init__(self, *, name: Optional[str] = None,
                  interval_seconds: Optional[float] = None,
                  tick_fn: Optional[Callable] = None,
                  logger: Optional[logging.Logger] = None) -> None:
        if name is not None:
            self.name = name
        if interval_seconds is not None:
            self.interval_seconds = interval_seconds
        self._tick_fn = tick_fn
        # Existing pawcorder loggers use dotted snake_case
        # (`pawcorder.pet_diary`); normalise dashes so a log-config
        # filter on `pawcorder.foo_bar` catches PollingTask-spawned
        # loggers too.
        logger_name = f"pawcorder.{self.name}".replace("-", "_")
        self._logger = logger or logging.getLogger(logger_name)
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def _tick(self) -> None:
        if self._tick_fn is not None:
            res = self._tick_fn()
            if asyncio.iscoroutine(res):
                await res
            return
        raise NotImplementedError(f"{self.name}: override _tick or pass tick_fn=")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name=self.name)
            self._logger.info("%s started", self.name)

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Signal stop and await the task. Falls back to cancel after
        `timeout` so shutdown can't hang forever on a stuck tick."""
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                # Per-tick swallow: one bad tick must not kill the
                # poller. Log and ride on to the next interval.
                self._logger.warning("%s tick failed: %s", self.name, exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(),
                                        timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass


async def read_capped_upload(file: "UploadFile", max_bytes: int) -> bytes:
    """Read an UploadFile in chunks, aborting as soon as we cross the cap.

    Plain `await file.read()` buffers the whole multipart into memory
    before any size check runs — that's an OOM vector for any public
    upload route. Instead we stream 64 KB at a time and throw
    UploadTooLarge once we've seen too much, leaving the rest unread
    so starlette discards it.

    Returns the bytes if size <= max_bytes, raises UploadTooLarge
    otherwise.
    """
    chunk_size = 64 * 1024
    out = bytearray()
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        if len(out) + len(chunk) > max_bytes:
            raise UploadTooLarge(
                f"upload exceeded {max_bytes} bytes (saw {len(out) + len(chunk)})"
            )
        out.extend(chunk)
    return bytes(out)
