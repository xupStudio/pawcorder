"""Re-run pet recognition over events that already happened.

Use case: user adds a pet today and wants the past week of unknown
sightings to retro-actively get labels. We pull every event in the
window from Frigate, fetch its snapshot, embed, match, and rewrite
the corresponding sighting in sightings.ndjson.

Runs as an asyncio background task with a single in-memory progress
record so the UI can poll for status. Only one backfill at a time —
new requests while one is running are rejected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from . import recognition
from .pets_store import PetStore
from .utils import atomic_write_text

logger = logging.getLogger("pawcorder.recognition_backfill")


@dataclass
class BackfillProgress:
    """Live state of a backfill run."""
    running: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    total_events: int = 0
    processed: int = 0
    matched: int = 0           # events that got a non-None pet_id
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_events": self.total_events,
            "processed": self.processed,
            "matched": self.matched,
            "error": self.error,
            "fraction": (self.processed / self.total_events) if self.total_events else 0.0,
        }


_progress = BackfillProgress()
_lock = asyncio.Lock()


def current_progress() -> BackfillProgress:
    return _progress


async def _fetch_events(since_hours: float, frigate_base: str) -> list[dict]:
    """Pull events with snapshots from Frigate. Limit is 1000 — past
    that the user almost certainly wants to do this in batches."""
    url = f"{frigate_base}/api/events"
    after = time.time() - since_hours * 3600
    params = {
        "after": int(after), "limit": 1000,
        "has_snapshot": 1, "include_thumbnails": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        return resp.json() or []
    except (httpx.HTTPError, ValueError):
        return []


async def _fetch_snapshot(event_id: str, frigate_base: str) -> Optional[bytes]:
    url = f"{frigate_base}/api/events/{event_id}/snapshot.jpg"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.content:
            return None
        return resp.content
    except httpx.HTTPError:
        return None


def _rewrite_sightings(updated: dict[str, dict]) -> int:
    """Update existing sighting rows in-place. Returns number rewritten."""
    if not recognition.SIGHTINGS_LOG.exists():
        return 0
    try:
        lines = recognition.SIGHTINGS_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    out: list[str] = []
    rewritten = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        eid = row.get("event_id")
        if eid in updated:
            row.update(updated[eid])
            rewritten += 1
        out.append(json.dumps(row, ensure_ascii=False))
    if rewritten:
        # Atomic write — append_sighting() may interleave with us, and a
        # crash mid-write would otherwise truncate the user's history.
        atomic_write_text(recognition.SIGHTINGS_LOG, "\n".join(out) + "\n")
    return rewritten


async def run_backfill(*, since_hours: float = 168.0,
                        frigate_base: Optional[str] = None) -> BackfillProgress:
    """One-shot backfill. Updates sightings.ndjson in-place AND appends
    new rows for events the polling loop never logged. Returns the
    final progress snapshot."""
    global _progress
    if _lock.locked():
        return _progress  # another run in flight
    async with _lock:
        from . import telegram as tg
        base = frigate_base or tg.FRIGATE_BASE_URL
        _progress = BackfillProgress(running=True, started_at=time.time())

        events = await _fetch_events(since_hours, base)
        _progress.total_events = len(events)
        if not events:
            _progress.running = False
            _progress.finished_at = time.time()
            return _progress

        store = PetStore()
        pets = store.load()
        if not pets:
            _progress.running = False
            _progress.finished_at = time.time()
            _progress.error = "no pets configured"
            return _progress

        # Existing event_ids → for "rewrite or append" decision.
        existing_ids = {r.get("event_id") for r in recognition.read_sightings(limit=20_000)
                        if r.get("event_id")}
        rewrites: dict[str, dict] = {}

        for event in events:
            try:
                event_id = str(event.get("id") or "")
                if not event_id:
                    continue
                snapshot = await _fetch_snapshot(event_id, base)
                if not snapshot:
                    _progress.processed += 1
                    continue
                match = recognition.match_against_pets(snapshot, pets)

                if event_id in existing_ids:
                    # Update in-place when our match adds info.
                    if match.pet_id:
                        rewrites[event_id] = {
                            "pet_id": match.pet_id,
                            "pet_name": match.pet_name,
                            "score": round(match.score, 4),
                            "confidence": match.confidence,
                        }
                else:
                    # Append fresh sighting.
                    recognition.append_sighting(recognition.Sighting(
                        event_id=event_id,
                        camera=str(event.get("camera") or "unknown"),
                        label=str(event.get("label") or "?"),
                        pet_id=match.pet_id,
                        pet_name=match.pet_name,
                        score=match.score,
                        confidence=match.confidence,
                        start_time=float(event.get("start_time") or 0),
                        end_time=float(event.get("end_time") or 0),
                    ))

                if match.pet_id:
                    _progress.matched += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("backfill iteration failed: %s", exc)
            finally:
                _progress.processed += 1

        if rewrites:
            try:
                _rewrite_sightings(rewrites)
            except Exception as exc:  # noqa: BLE001
                logger.warning("backfill rewrite failed: %s", exc)

        _progress.running = False
        _progress.finished_at = time.time()
        return _progress
