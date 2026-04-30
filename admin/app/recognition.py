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

# Heuristic prior weights — kicked in for multi-pet households where
# two pets have similar embeddings (e.g. two black cats). All four
# signals are additive bumps on the raw cosine score; total budget is
# capped so a confidently-correct ID can never get overruled by priors.
# Values picked conservatively from manual A/B on a 5-cat dataset.
PRIOR_WEIGHT_TIME = 0.05      # hour-of-day match
PRIOR_WEIGHT_CAMERA = 0.05    # which camera the pet usually appears on
PRIOR_WEIGHT_INERTIA = 0.06   # last seen on same camera <60s ago
PRIOR_WEIGHT_SIZE = 0.04      # bbox area within pet's typical range
PRIOR_TOTAL_CAP = 0.12        # ceiling on combined boost — never flip
                              # a high-cosine match without a good cosine
                              # reason. (Sum of weights would be 0.20;
                              # we deliberately under-budget.)
INERTIA_WINDOW_SECONDS = 60   # "same activity" window for identity inertia
PRIOR_HISTORY_DAYS = 14       # rolling window for prior histograms
PRIOR_CACHE_TTL_SECONDS = 600 # rebuild priors every 10 min — cheap


@dataclass
class MatchResult:
    """Outcome of one event-to-pet match attempt."""
    pet_id: Optional[str]   # None if no acceptable match
    pet_name: Optional[str] # display name, denormalized for log readability
    score: float            # best (post-prior) score
    confidence: str         # "high" | "tentative" | "unknown"
    reason: str = ""        # for diagnostics; never shown to user
    # Diagnostics for the multi-pet weighting path. None for single-pet
    # matches where priors aren't applied.
    cosine_only: Optional[float] = None    # raw cosine before priors
    prior_boost: Optional[float] = None    # total additive boost applied


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
    bbox: Optional[tuple[float, float, float, float]] = None  # [x, y, w, h] from Frigate, if known


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
    payload = {
        "event_id": s.event_id,
        "camera": s.camera,
        "label": s.label,
        "pet_id": s.pet_id,
        "pet_name": s.pet_name,
        "score": round(s.score, 4),
        "confidence": s.confidence,
        "start_time": s.start_time,
        "end_time": s.end_time,
    }
    if s.bbox is not None:
        # Persist bbox so future prior-builds can compute per-pet, per-
        # camera size distributions. Skip when unknown (older callers,
        # Frigate events without `data.box`). json.dumps handles a tuple
        # the same as a list — no need to allocate a copy.
        payload["bbox"] = s.bbox
    line = json.dumps(payload, ensure_ascii=False)
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
    from .utils import read_ndjson
    return read_ndjson(
        SIGHTINGS_LOG,
        filter_fn=lambda r: r.get("start_time", 0) >= since,
        sort_key=lambda r: r.get("start_time", 0),
        reverse=True,
        limit=limit,
    )


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


def extract_bbox_from_event(event: dict) -> Optional[tuple[float, float, float, float]]:
    """Pull the [x, y, w, h] bbox out of a Frigate event payload.

    Frigate's API has shifted the bbox location across versions and
    states:
      * >= 0.13:  ``event["data"]["box"]`` — current detection
      * older:    top-level ``event["box"]``
      * in-progress events sometimes only have ``event["data"]["region"]``
        (the broader bounding box used for inference) before a tight
        detection box has settled
    Returns None when the event has no usable bbox (also covers
    NULL / non-numeric entries — we silently fall through rather
    than crash a recognition pass over a single malformed event).
    """
    data = event.get("data") if isinstance(event.get("data"), dict) else None
    raw = ((data or {}).get("box")
           or event.get("box")
           or (data or {}).get("region"))
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        return tuple(float(v) for v in raw[:4])
    except (TypeError, ValueError):
        return None


# ---- multi-pet heuristic priors ---------------------------------------
#
# The raw cosine score works perfectly for one pet and breaks down for
# 2+ visually similar pets (e.g. two black cats). To break ties we
# layer in four cheap signals on top of cosine:
#
#   * **Time-of-day prior** — each pet has a 24-bin hour histogram
#     built from past sightings. Mochi at 03:00 is a low prior because
#     she's never been seen at 3am; Maru at 03:00 might be a high prior.
#   * **Camera prior** — each pet has a per-camera frequency. The
#     bedroom cat doesn't usually appear on the front-door camera.
#   * **Identity inertia** — if the same camera saw a confidently-
#     identified pet within INERTIA_WINDOW_SECONDS, the next event is
#     likely the same pet (it's still in frame).
#   * **Size prior** — bbox area lives within a per-pet, per-camera
#     range (perspective-dependent, but stable for one camera). A
#     bbox 2× the median for Mochi is a strong "not Mochi" signal.
#
# All four are *additive* on top of cosine, capped at PRIOR_TOTAL_CAP
# so a confident cosine match can never get overridden. Each prior
# returns a value in roughly [-w, +w]; sum is clamped to [-cap, +cap].

@dataclass
class _PriorCache:
    """Per-process cache of prior histograms, rebuilt every TTL seconds."""
    built_at: float = 0.0
    by_pet: dict[str, dict] = None  # type: ignore[assignment]


_prior_cache = _PriorCache()
_prior_lock = threading.Lock()


def _build_priors(*, now: Optional[float] = None) -> dict[str, dict]:
    """Build per-pet histograms from the last PRIOR_HISTORY_DAYS of sightings.

    Returns a {pet_id: {hour: histogram, camera: histogram, size: dict-by-cam}}
    map. Pets with no history get an empty entry → priors all return 0
    for them (no boost, no penalty — fall back to pure cosine).
    """
    from .utils import read_ndjson
    now = now or time.time()
    cutoff = now - PRIOR_HISTORY_DAYS * 86400
    # Only confident, identified rows feed the priors. Pushing the
    # filter into the scan avoids materialising tens of thousands of
    # tentative/unknown rows just to throw them away.
    rows = read_ndjson(
        SIGHTINGS_LOG,
        filter_fn=lambda r: (r.get("confidence") == "high"
                              and bool(r.get("pet_id"))
                              and (r.get("start_time") or 0) >= cutoff),
        limit=20_000,
    )
    out: dict[str, dict] = {}
    for r in rows:
        pid = r.get("pet_id")
        slot = out.setdefault(pid, {"hour": [0] * 24, "camera": {}, "size": {}})
        ts = float(r.get("start_time") or 0)
        if ts > 0:
            slot["hour"][time.localtime(ts).tm_hour] += 1
        cam = str(r.get("camera") or "")
        if cam:
            slot["camera"][cam] = slot["camera"].get(cam, 0) + 1
        # bbox area (if logged) — we don't always have it, so default to None
        bbox = r.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4 and cam:
            try:
                area = float(bbox[2]) * float(bbox[3])
            except (TypeError, ValueError):
                area = 0.0
            if area > 0:
                slot["size"].setdefault(cam, []).append(area)
    return out


def _get_priors(*, now: Optional[float] = None) -> dict[str, dict]:
    """Return cached priors, rebuilding if stale."""
    now = now or time.time()
    with _prior_lock:
        if (_prior_cache.by_pet is None
                or now - _prior_cache.built_at > PRIOR_CACHE_TTL_SECONDS):
            _prior_cache.by_pet = _build_priors(now=now)
            _prior_cache.built_at = now
        return _prior_cache.by_pet


def _time_prior(pet_id: str, hour: int, priors: dict[str, dict]) -> float:
    """0 if no history, otherwise +PRIOR_WEIGHT_TIME × normalized affinity.

    "Normalized affinity" means: how much of this pet's hourly activity
    landed in the given hour, vs. uniform. A bin with 4× the average
    rate gets the full weight; a never-seen-in-this-hour bin gets the
    full negative weight.
    """
    hist = priors.get(pet_id, {}).get("hour")
    if not hist or sum(hist) == 0:
        return 0.0
    total = sum(hist)
    p_hour = hist[hour] / total            # observed mass at this hour
    expected = 1.0 / 24                    # uniform baseline
    # Map p_hour ∈ [0, 1] → [-1, +1] around the uniform baseline,
    # squashed via tanh so dominant hours don't dominate the score.
    delta = (p_hour - expected) / max(expected, 1e-6)
    return PRIOR_WEIGHT_TIME * float(np.tanh(delta))


def _camera_prior(pet_id: str, camera: str, priors: dict[str, dict]) -> float:
    """Same shape as time prior but over camera names instead of hours."""
    hist = priors.get(pet_id, {}).get("camera")
    if not hist:
        return 0.0
    total = sum(hist.values())
    if total == 0:
        return 0.0
    p_cam = hist.get(camera, 0) / total
    n_cams = max(len(hist), 1)
    expected = 1.0 / n_cams
    delta = (p_cam - expected) / max(expected, 1e-6)
    return PRIOR_WEIGHT_CAMERA * float(np.tanh(delta))


def _size_prior(pet_id: str, camera: str, bbox_area: Optional[float],
                priors: dict[str, dict]) -> float:
    """Bbox area within ±1σ of pet's mean → +; >2σ away → −.
    Returns 0 if no history or no bbox provided."""
    if not bbox_area or bbox_area <= 0:
        return 0.0
    sizes = priors.get(pet_id, {}).get("size", {}).get(camera)
    if not sizes or len(sizes) < 3:   # need a few samples to mean anything
        return 0.0
    mean = sum(sizes) / len(sizes)
    if mean <= 0:
        return 0.0
    var = sum((s - mean) ** 2 for s in sizes) / len(sizes)
    std = var ** 0.5
    if std <= 0:
        return 0.0
    z = abs(bbox_area - mean) / std
    # z=0 → +1, z=1 → 0, z>=2 → -1, smooth via piecewise linear.
    affinity = max(-1.0, 1.0 - z)
    return PRIOR_WEIGHT_SIZE * affinity


def _inertia_prior(pet_id: str, camera: str, *,
                    recent_rows: list[dict]) -> float:
    """+PRIOR_WEIGHT_INERTIA if `recent_rows` (already filtered to the
    inertia window) holds a confident sighting of this pet on this
    camera. Caller hoists the read out of the per-pet loop to avoid
    re-opening the sightings log once per candidate pet."""
    for r in recent_rows:
        if (r.get("camera") == camera
                and r.get("pet_id") == pet_id
                and r.get("confidence") == "high"):
            return PRIOR_WEIGHT_INERTIA
    return 0.0


def _apply_priors(cosines: list[tuple[str, str, float]], *,
                   camera: str, hour: int, now: float,
                   bbox_area: Optional[float] = None,
                   priors: Optional[dict[str, dict]] = None
                   ) -> list[tuple[str, str, float, float]]:
    """For each (pet_id, pet_name, cosine), compute boosted score.

    Returns list of (pet_id, pet_name, cosine, boosted) tuples sorted
    by boosted score descending. The boost is clamped to ±PRIOR_TOTAL_CAP.
    """
    priors = priors if priors is not None else _get_priors(now=now)
    # Read inertia window once — without this each candidate pet would
    # re-open sightings.ndjson, turning the per-event match into a
    # P-file-reads N+1.
    recent_rows = read_sightings(limit=20, since=now - INERTIA_WINDOW_SECONDS)
    out: list[tuple[str, str, float, float]] = []
    for pet_id, pet_name, cos in cosines:
        boost = (
            _time_prior(pet_id, hour, priors)
            + _camera_prior(pet_id, camera, priors)
            + _size_prior(pet_id, camera, bbox_area, priors)
            + _inertia_prior(pet_id, camera, recent_rows=recent_rows)
        )
        boost = max(-PRIOR_TOTAL_CAP, min(PRIOR_TOTAL_CAP, boost))
        out.append((pet_id, pet_name, cos, cos + boost))
    out.sort(key=lambda x: x[3], reverse=True)
    return out


def match_with_priors(snapshot_bytes: bytes, pets: list[Pet], *,
                       camera: str, now: Optional[float] = None,
                       bbox_area: Optional[float] = None) -> MatchResult:
    """Same as match_against_pets, but with multi-pet heuristic re-ranking.

    Falls back to vanilla matching if there's only one pet (no ranking
    to do) or no prior history (priors all return 0 → identical result).
    """
    now = now or time.time()
    if len(pets) <= 1:
        return match_against_pets(snapshot_bytes, pets)

    extractor = embeddings.get_extractor()
    result = extractor.extract(snapshot_bytes)
    if not result.success:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason=result.error)

    # Aggregate cosine per pet (max across that pet's reference photos).
    per_pet_cos: dict[str, tuple[str, float]] = {}  # pet_id -> (pet_name, max_cos)
    for p in pets:
        best = -1.0
        for ph in p.photos:
            if len(ph.embedding) != embeddings.EMBEDDING_DIM:
                continue
            v = np.asarray(ph.embedding, dtype=np.float32)
            sim = float(v @ result.vector)
            if sim > best:
                best = sim
        if best > -1.0:
            per_pet_cos[p.pet_id] = (p.name, best)

    if not per_pet_cos:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason="no reference embeddings")

    cosines = [(pid, name, cos) for pid, (name, cos) in per_pet_cos.items()]
    hour = time.localtime(now).tm_hour
    ranked = _apply_priors(cosines, camera=camera, hour=hour, now=now,
                           bbox_area=bbox_area)
    pet_id, pet_name, cos_only, boosted = ranked[0]

    if boosted >= HIGH_CONFIDENCE:
        confidence = "high"
    elif boosted >= MATCH_THRESHOLD:
        confidence = "tentative"
    else:
        return MatchResult(pet_id=None, pet_name=None, score=boosted,
                           confidence="unknown",
                           reason=f"top boosted score {boosted:.3f} below {MATCH_THRESHOLD}",
                           cosine_only=cos_only, prior_boost=boosted - cos_only)

    return MatchResult(
        pet_id=pet_id, pet_name=pet_name,
        score=boosted, confidence=confidence,
        cosine_only=cos_only, prior_boost=boosted - cos_only,
    )


# ---- glue: identify a Frigate event ------------------------------------

def identify_event(snapshot_bytes: bytes, *, event_id: str, camera: str,
                   label: str, start_time: float, end_time: float = 0.0,
                   bbox: Optional[tuple[float, float, float, float]] = None,
                   pet_store: PetStore | None = None) -> MatchResult:
    """One call from the Frigate event poller: embed, match, log, return.

    For multi-pet households (≥2 pets) we route through the heuristic
    matcher, which uses time/camera/size/inertia priors to break cosine
    ties. Single-pet installs use the original matcher (no priors needed).

    Returns the MatchResult so the caller (telegram.py) can include the
    pet name in the notification text.
    """
    store = pet_store or PetStore()
    pets = store.load()
    bbox_area: Optional[float] = None
    if bbox is not None and len(bbox) >= 4:
        try:
            bbox_area = float(bbox[2]) * float(bbox[3])
        except (TypeError, ValueError):
            bbox_area = None
    if len(pets) >= 2:
        result = match_with_priors(
            snapshot_bytes, pets, camera=camera,
            now=start_time or None, bbox_area=bbox_area,
        )
    else:
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
        bbox=tuple(bbox) if bbox is not None else None,
    )
    try:
        append_sighting(sighting)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to log sighting %s: %s", event_id, exc)
    return result
