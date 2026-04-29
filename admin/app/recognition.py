"""Match a Frigate event snapshot against the user's known pets.

Pipeline per event:
  1. Pull the snapshot bytes (already done by telegram.py poller).
  2. Embed via embeddings.get_extractor().
  3. Compare against every stored PetPhoto.embedding.
  4. If best similarity ≥ MATCH_THRESHOLD, label the event with that
     pet's pet_id; otherwise label as 'unknown'.

We persist the match result to a small NDJSON log (one event per line)
so the /pets page can show "Mochi: 47 sightings today" without re-
embedding old events.

Soft-fails everywhere: if onnxruntime isn't installed, model isn't
downloaded, or the event has no snapshot yet — we just skip and the
rest of the admin keeps working.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import embeddings
from .pets_store import Pet, PetStore

logger = logging.getLogger("pawcorder.recognition")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
SIGHTINGS_LOG = DATA_DIR / "config" / "sightings.ndjson"

# Cosine-similarity threshold above which we accept a label. 0.78 is
# tuned against MobileNetV3-Small embeddings — high enough that
# random-cat shots don't flip-flop, low enough that "Mochi from a new
# angle" still matches against the reference photos.
MATCH_THRESHOLD = 0.78
# Above this, we're very confident; below MATCH_THRESHOLD we say
# "unknown". Between is "tentative" — UI can show with reduced opacity.
HIGH_CONFIDENCE = 0.88

# Cap the on-disk log so an always-on system doesn't grow unbounded.
# 365 days × ~50 events/day × 200 bytes ≈ 3.6 MB, comfortable.
MAX_LOG_LINES = 50_000


@dataclass
class MatchResult:
    """Outcome of one event-to-pet match attempt."""
    pet_id: Optional[str]   # None if no acceptable match
    pet_name: Optional[str] # display name, denormalized for log readability
    score: float            # best cosine similarity seen
    confidence: str         # "high" | "tentative" | "unknown"
    reason: str = ""        # for diagnostics; never shown to user


@dataclass
class Sighting:
    """One row in the sightings log. Keep small — we write a lot."""
    event_id: str
    camera: str
    label: str              # 'cat' / 'dog' from Frigate
    pet_id: Optional[str]
    pet_name: Optional[str]
    score: float
    confidence: str
    start_time: float       # unix seconds
    end_time: float         # 0 if event still ongoing


# ---- core matcher -------------------------------------------------------

def match_against_pets(snapshot_bytes: bytes, pets: list[Pet]) -> MatchResult:
    """Embed once, compare against every pet's reference photos.

    With L2-normalized embeddings cosine_similarity ≡ dot product, so
    we vectorize the whole comparison in a single matmul.
    """
    if not pets:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason="no pets configured")

    extractor = embeddings.get_extractor()
    result = extractor.extract(snapshot_bytes)
    if not result.success:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason=result.error)

    # Build a (N, D) matrix of every reference embedding plus a parallel
    # list of (pet_id, pet_name) so we can recover which pet won.
    flat: list[tuple[str, str, np.ndarray]] = []
    for p in pets:
        for ph in p.photos:
            if len(ph.embedding) == embeddings.EMBEDDING_DIM:
                flat.append((p.pet_id, p.name, np.asarray(ph.embedding, dtype=np.float32)))
    if not flat:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason="no reference embeddings")

    matrix = np.stack([row[2] for row in flat])  # shape (N, D)
    sims = matrix @ result.vector                # shape (N,)
    best_idx = int(np.argmax(sims))
    best_score = float(sims[best_idx])
    best_pet_id, best_pet_name, _ = flat[best_idx]

    if best_score >= HIGH_CONFIDENCE:
        return MatchResult(pet_id=best_pet_id, pet_name=best_pet_name,
                           score=best_score, confidence="high")
    if best_score >= MATCH_THRESHOLD:
        return MatchResult(pet_id=best_pet_id, pet_name=best_pet_name,
                           score=best_score, confidence="tentative")
    return MatchResult(pet_id=None, pet_name=None, score=best_score,
                       confidence="unknown",
                       reason=f"top score {best_score:.3f} below {MATCH_THRESHOLD}")


# ---- sightings log -----------------------------------------------------

_log_lock = threading.Lock()


def append_sighting(s: Sighting) -> None:
    """Append-only NDJSON log. We don't need atomic writes here:
       - one event = one line, written via a single os.write
       - if the process is killed mid-line, the partial line is the
         last in the file and gets re-truncated by the next rotate.
    """
    SIGHTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "event_id": s.event_id,
        "camera": s.camera,
        "label": s.label,
        "pet_id": s.pet_id,
        "pet_name": s.pet_name,
        "score": round(s.score, 4),
        "confidence": s.confidence,
        "start_time": s.start_time,
        "end_time": s.end_time,
    }, ensure_ascii=False)
    with _log_lock:
        with SIGHTINGS_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        _maybe_rotate()


def _maybe_rotate() -> None:
    """If the log is over MAX_LOG_LINES, drop the oldest half. Cheap to
    run on every append because we only count when the file is large."""
    try:
        size = SIGHTINGS_LOG.stat().st_size
    except OSError:
        return
    # Heuristic — average line is ~200 bytes so 50k × 200 = 10 MB.
    if size < MAX_LOG_LINES * 250:
        return
    try:
        with SIGHTINGS_LOG.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_LOG_LINES:
        return
    keep = lines[-(MAX_LOG_LINES // 2):]
    SIGHTINGS_LOG.write_text("".join(keep), encoding="utf-8")


def read_sightings(*, limit: int = 500, since: float = 0.0) -> list[dict]:
    """Tail of the log, newest-first. limit caps the slice; since filters
    by start_time. Used by /api/pets/{id}/timeline."""
    if not SIGHTINGS_LOG.exists():
        return []
    out: list[dict] = []
    try:
        with SIGHTINGS_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("start_time", 0) >= since:
                    out.append(row)
    except OSError:
        return []
    out.sort(key=lambda r: r.get("start_time", 0), reverse=True)
    return out[:limit]


def stats_for_pet(pet_id: str, *, since_hours: float = 24.0) -> dict:
    """Quick summary for the /pets list page: sightings count, last seen."""
    cutoff = time.time() - since_hours * 3600
    rows = [r for r in read_sightings(limit=10_000, since=cutoff)
            if r.get("pet_id") == pet_id]
    if not rows:
        return {"sightings": 0, "last_seen": None, "cameras": []}
    return {
        "sightings": len(rows),
        "last_seen": rows[0].get("start_time"),
        "cameras": sorted({r.get("camera") for r in rows if r.get("camera")}),
    }


# ---- glue: identify a Frigate event ------------------------------------

def identify_event(snapshot_bytes: bytes, *, event_id: str, camera: str,
                   label: str, start_time: float, end_time: float = 0.0,
                   pet_store: PetStore | None = None) -> MatchResult:
    """One call from the Frigate event poller: embed, match, log, return.

    Returns the MatchResult so the caller (telegram.py) can include the
    pet name in the notification text.
    """
    store = pet_store or PetStore()
    pets = store.load()
    result = match_against_pets(snapshot_bytes, pets)
    sighting = Sighting(
        event_id=event_id,
        camera=camera,
        label=label,
        pet_id=result.pet_id,
        pet_name=result.pet_name,
        score=result.score,
        confidence=result.confidence,
        start_time=start_time,
        end_time=end_time,
    )
    try:
        append_sighting(sighting)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to log sighting %s: %s", event_id, exc)
    return result
