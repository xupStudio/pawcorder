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
# Cloud-trained per-pet classifier files. ``cloud_train.py`` writes
# them here; recognition reads them only to confirm presence + magic
# header for the UI badge. Real classifier inference lives in a
# follow-up swap-in — see ``cloud_train._train_stub`` for the file's
# placeholder shape.
CLOUD_MODELS_DIR = DATA_DIR / "models"
CLOUD_MODEL_MAGIC = b"PWCDRMDL1"

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
    # Multi-frame diagnostics — populated when the caller fed >1 snapshot
    # to identify_event. Surfaced on /pets so the user sees that quality
    # pooling actually ran ("3 of 4 frames usable").
    frames_used: int = 1
    frames_offered: int = 1


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
    # 1 if the matcher saw a single snapshot, 2+ if multi-frame quality
    # pooling kicked in. Persisted so the /pets/health page can show
    # "47 of today's 60 events used multi-frame" — a concrete signal
    # that the upgrade is doing something.
    frames_used: int = 1


# ---- cloud-trained per-pet classifiers ---------------------------------
#
# When the owner has run cloud-train for a pet, we land a small model
# file at ``<data>/models/petclf-<pet>.joblib``. Two on-disk versions
# exist:
#
#   * ``PWCDRMDL1`` — placeholder (legacy; relay before kernel was
#     wired). Treated as "this pet has a model" for the UI badge but
#     falls through to baseline cosine matching at inference.
#   * ``PWCDRMDL2`` — real classifier (prototype + Gaussian over cosine
#     distance — see relay/cloud_train_kernel.py for the math). Loaded
#     into memory and queried alongside cosine matching to produce a
#     probability we report on the /pets page.
#
# Both are kept under the same path/magic-prefix scheme; recognition
# probes the magic and routes accordingly.

_PET_ID_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
CLOUD_MODEL_MAGIC_V2 = b"PWCDRMDL2"

# Per-(pet_id, mtime) cache. mtime invalidates after a re-train so the
# admin doesn't keep using the stale model after a fresh download.
_cloud_model_cache: dict[str, tuple[float, object]] = {}
_cloud_model_lock = threading.Lock()


def has_cloud_model(pet_id: str) -> bool:
    """True iff a valid (magic-header) cloud-train model file exists
    for this pet. Used by /pets and the train page to badge a pet as
    "custom-trained"."""
    # Defensive shape check — a future migration that loosened slug
    # rules shouldn't accidentally turn this into an ``open()`` of an
    # arbitrary file. ``False`` for malformed input is the safe answer.
    if not isinstance(pet_id, str) or not _PET_ID_RE.match(pet_id):
        return False
    path = CLOUD_MODELS_DIR / f"petclf-{pet_id}.joblib"
    if not path.exists():
        return False
    try:
        with path.open("rb") as fh:
            magic = fh.read(len(CLOUD_MODEL_MAGIC))
        # Accept both the legacy placeholder and the V2 real classifier
        # for the UI badge — owners who trained recently see "trained",
        # owners on legacy stub see the same.
        return magic == CLOUD_MODEL_MAGIC or magic == CLOUD_MODEL_MAGIC_V2
    except OSError:
        return False


def cloud_model_status(pet_id: str) -> dict:
    """Surface the on-disk model state for the /pets/{id}/train-cloud
    page. Returns ``{kind, trained_at, n_positives}`` or ``{kind: 'none'}``.

    'real' = V2 classifier loaded; 'placeholder' = V1 stub; 'none' = no
    file. Plain-language UI copy translates these for the owner.
    """
    if not isinstance(pet_id, str) or not _PET_ID_RE.match(pet_id):
        return {"kind": "none"}
    path = CLOUD_MODELS_DIR / f"petclf-{pet_id}.joblib"
    if not path.exists():
        return {"kind": "none"}
    try:
        magic = path.open("rb").read(len(CLOUD_MODEL_MAGIC))
    except OSError:
        return {"kind": "none"}
    if magic == CLOUD_MODEL_MAGIC_V2:
        cm = _load_cloud_model(pet_id)
        if cm is None:
            return {"kind": "real", "trained_at": None, "n_positives": 0}
        return {
            "kind": "real",
            "trained_at": getattr(cm, "trained_at", 0.0),
            "n_positives": getattr(cm, "n_positives", 0),
            "distance_mean": round(float(getattr(cm, "distance_mean", 0.0)), 4),
            "distance_std": round(float(getattr(cm, "distance_std", 0.0)), 4),
        }
    if magic == CLOUD_MODEL_MAGIC:
        return {"kind": "placeholder"}
    return {"kind": "none"}


def _load_cloud_model(pet_id: str):
    """Return the deserialised CloudModel for this pet, or None.

    Cached per (pet_id, mtime). The kernel module is imported lazily
    so the admin doesn't pay the joblib/numpy cost when the owner
    hasn't enabled cloud training.
    """
    if not isinstance(pet_id, str) or not _PET_ID_RE.match(pet_id):
        return None
    path = CLOUD_MODELS_DIR / f"petclf-{pet_id}.joblib"
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _cloud_model_lock:
        cached = _cloud_model_cache.get(pet_id)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    if not blob.startswith(CLOUD_MODEL_MAGIC_V2):
        # V1 placeholder or corrupted. Caller falls back to cosine.
        return None
    try:
        # Lazy import — the admin pays joblib's startup cost only the
        # first time a real cloud model is queried.
        import importlib
        import sys
        # The kernel ships with the relay package. Admin doesn't import
        # the relay app itself, but the kernel is a stand-alone module
        # — the install path puts it on PYTHONPATH so this works in
        # dev (monorepo) and prod (separate wheel) alike.
        try:
            kernel = importlib.import_module("relay.cloud_train_kernel")
        except ImportError:
            try:
                kernel = importlib.import_module("cloud_train_kernel")
            except ImportError:
                logger.debug("cloud_train_kernel not on path — skipping V2 load")
                return None
        model = kernel.deserialize(blob)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cloud model load failed for %s: %s", pet_id, exc)
        return None
    with _cloud_model_lock:
        _cloud_model_cache[pet_id] = (mtime, model)
    return model


def stale_cloud_models() -> list[str]:
    """Return pet_ids whose on-disk V2 cloud model fails to deserialize.

    A backbone swap (System page → Recognition model) leaves V2 models
    trained against the old backbone unreadable — :func:`_load_cloud_model`
    returns None for them and ``_cloud_boost`` becomes a no-op. The
    custom training the operator paid for is silently disabled until
    they re-upload photos under the new backbone.

    This helper counts those pets so the UI can warn — without it the
    operator has no way to notice the regression. Returns the list of
    pet_ids with stale models so callers can both render a count and
    link to specific train-cloud pages.
    """
    out: list[str] = []
    if not CLOUD_MODELS_DIR.exists():
        return out
    for path in CLOUD_MODELS_DIR.iterdir():
        if not path.name.startswith("petclf-") or path.suffix != ".joblib":
            continue
        pet_id = path.name[len("petclf-"):-len(".joblib")]
        if not _PET_ID_RE.match(pet_id):
            continue
        # Only V2 magic can be stale — V1 placeholders are stale by
        # definition (they predate the kernel) but we don't count them
        # as a *new* problem caused by the backbone swap.
        try:
            with path.open("rb") as fh:
                head = fh.read(len(CLOUD_MODEL_MAGIC_V2))
        except OSError:
            continue
        if head != CLOUD_MODEL_MAGIC_V2:
            continue
        if _load_cloud_model(pet_id) is None:
            out.append(pet_id)
    return out


def _cloud_score(pet_id: str, embedding: np.ndarray) -> Optional[float]:
    """Probability ``embedding`` belongs to ``pet_id`` per the trained
    cloud model. ``None`` if no V2 model is available — caller falls
    back to cosine."""
    model = _load_cloud_model(pet_id)
    if model is None:
        return None
    try:
        import importlib
        try:
            kernel = importlib.import_module("relay.cloud_train_kernel")
        except ImportError:
            kernel = importlib.import_module("cloud_train_kernel")
        return float(kernel.score(model, embedding))
    except Exception as exc:  # noqa: BLE001
        logger.debug("cloud score failed for %s: %s", pet_id, exc)
        return None


# Maximum additive boost the cloud-trained classifier can contribute on
# top of cosine. Same conservative posture as the heuristic priors —
# capped low enough that a strong cosine match can never be flipped by
# the cloud score alone, but high enough to break ties between two pets
# with similar cosine scores.
CLOUD_BOOST_CAP = 0.08


def _cloud_boost(pet_id: str, embedding: np.ndarray) -> float:
    """Convert cloud probability to an additive cosine adjustment.

    cloud_score ∈ (0, 1), centred at 0.5 (= "training median"). We map
    ±0.5 of probability range to ±CLOUD_BOOST_CAP. ``0`` when no model
    is loaded — current cosine behaviour is preserved exactly.
    """
    cs = _cloud_score(pet_id, embedding)
    if cs is None:
        return 0.0
    # (cs - 0.5) ∈ [-0.5, 0.5] → scaled by 2*CAP → [-CAP, +CAP]
    return float((cs - 0.5) * 2.0 * CLOUD_BOOST_CAP)


# ---- core matcher -------------------------------------------------------

def _embed_one_or_many(snapshot_or_frames: bytes | list[bytes]) -> tuple[
        Optional[np.ndarray], int, int, str]:
    """Single embedding step shared by single- and multi-frame matchers.

    Returns (vector_or_None, frames_used, frames_offered, error_msg).
    A bytes input is treated as a 1-frame batch so both code paths agree
    on error handling. The vector is L2-normalized; downstream code can
    treat it as a unit-length 1-D ndarray.
    """
    extractor = embeddings.get_extractor()
    if isinstance(snapshot_or_frames, (list, tuple)):
        frames = list(snapshot_or_frames)
        if not frames:
            return None, 0, 0, "no frames"
        if len(frames) == 1:
            r = extractor.extract(frames[0])
            return (r.vector if r.success else None), (1 if r.success else 0), 1, r.error
        m = extractor.extract_many(frames)
        return (m.vector if m.success else None), m.frame_count, len(frames), m.error
    r = extractor.extract(snapshot_or_frames)
    return (r.vector if r.success else None), (1 if r.success else 0), 1, r.error


def match_against_pets(snapshot_bytes: bytes | list[bytes],
                        pets: list[Pet]) -> MatchResult:
    """Embed once (or pool many), compare against every pet's references.

    With L2-normalized embeddings cosine_similarity ≡ dot product, so
    we vectorize the whole comparison in a single matmul. Accepts either
    a single snapshot (legacy callers) or a list of frames from one event
    — multi-frame inputs are quality-weighted before matching.
    """
    if not pets:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason="no pets configured")

    vec, frames_used, frames_offered, err = _embed_one_or_many(snapshot_bytes)
    if vec is None:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason=err,
                           frames_used=frames_used, frames_offered=frames_offered)

    # Build a (N, D) matrix of every reference embedding plus a parallel
    # list of (pet_id, pet_name) so we can recover which pet won. We
    # filter on BOTH dim *and* backbone name — two backbones with the
    # same dim (e.g. multiple 384-d ViTs) live in different feature
    # spaces, and silently mixing would produce nonsense cosines.
    active = embeddings.active_backbone_name()
    flat: list[tuple[str, str, np.ndarray]] = []
    for p in pets:
        for ph in p.photos:
            if (len(ph.embedding) == embeddings.EMBEDDING_DIM
                    and ph.backbone == active):
                flat.append((p.pet_id, p.name, np.asarray(ph.embedding, dtype=np.float32)))
    if not flat:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason="no reference embeddings",
                           frames_used=frames_used, frames_offered=frames_offered)

    matrix = np.stack([row[2] for row in flat])  # shape (N, D)
    sims = matrix @ vec                          # shape (N,)

    # Per-pet cloud-model boost. Computed once per unique pet_id (not per
    # reference photo) so a pet with 50 references doesn't pay 50 cloud
    # lookups. The boost is small (±0.08 max) so cosine still drives the
    # decision when there's no cloud model — current behaviour preserved.
    seen_pids: set[str] = set()
    boosts: dict[str, float] = {}
    for pid, _name, _vec in flat:
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        boosts[pid] = _cloud_boost(pid, vec)
    if boosts:
        adjusted = np.array(
            [sims[i] + boosts.get(flat[i][0], 0.0) for i in range(len(flat))],
            dtype=np.float32,
        )
    else:
        adjusted = sims

    best_idx = int(np.argmax(adjusted))
    best_score = float(adjusted[best_idx])
    best_pet_id, best_pet_name, _ = flat[best_idx]
    cosine_only = float(sims[best_idx])

    # Per-pet calibrated threshold trumps the global one if set. The
    # pet's `match_threshold` field is 0.0 when uncalibrated; we
    # fall back to MATCH_THRESHOLD so behaviour is unchanged for
    # everyone who hasn't run calibration.
    pet_threshold = MATCH_THRESHOLD
    for p in pets:
        if p.pet_id == best_pet_id and p.match_threshold > 0:
            pet_threshold = float(p.match_threshold)
            break

    cloud_delta = best_score - cosine_only
    if best_score >= HIGH_CONFIDENCE:
        return MatchResult(pet_id=best_pet_id, pet_name=best_pet_name,
                           score=best_score, confidence="high",
                           cosine_only=cosine_only if cloud_delta else None,
                           prior_boost=cloud_delta if cloud_delta else None,
                           frames_used=frames_used, frames_offered=frames_offered)
    if best_score >= pet_threshold:
        return MatchResult(pet_id=best_pet_id, pet_name=best_pet_name,
                           score=best_score, confidence="tentative",
                           cosine_only=cosine_only if cloud_delta else None,
                           prior_boost=cloud_delta if cloud_delta else None,
                           frames_used=frames_used, frames_offered=frames_offered)
    return MatchResult(pet_id=None, pet_name=None, score=best_score,
                       confidence="unknown",
                       reason=f"top score {best_score:.3f} below {pet_threshold}",
                       cosine_only=cosine_only if cloud_delta else None,
                       prior_boost=cloud_delta if cloud_delta else None,
                       frames_used=frames_used, frames_offered=frames_offered)


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
    if s.frames_used > 1:
        # Only persist when multi-frame actually fired. Single-frame is
        # the default; omitting saves ~12 bytes per row × 50k rows.
        payload["frames_used"] = s.frames_used
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


def daily_buckets(rows: list[dict], *, pet_id: str,
                    predicate=None, now: float, days: int) -> list[int]:
    """Bucket sightings by local-day for one pet, return ``days`` ints
    oldest-first.

    Several health surfaces (vet pack, /pets/health, bowl monitor,
    litter monitor) used to hand-roll this loop; they all want the
    same shape: filter to a pet, optionally apply a per-row predicate
    (e.g. "is this row inside the litter-box polygon?"), bucket by
    ``time.localtime`` day-key, and emit one count per day in the
    window. Centralising it avoids drift between charts that should
    agree (the litter sparkline on /pets/health and the litter chart
    in the vet pack must report the same number for the same day).
    """
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("pet_id") != pet_id:
            continue
        if predicate is not None and not predicate(r):
            continue
        ts = float(r.get("start_time") or 0)
        if ts <= 0:
            continue
        key = time.strftime("%Y-%m-%d", time.localtime(ts))
        counts[key] = counts.get(key, 0) + 1
    return [
        counts.get(time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)), 0)
        for i in range(days - 1, -1, -1)
    ]


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


def match_with_priors(snapshot_bytes: bytes | list[bytes], pets: list[Pet], *,
                       camera: str, now: Optional[float] = None,
                       bbox_area: Optional[float] = None) -> MatchResult:
    """Same as match_against_pets, but with multi-pet heuristic re-ranking.

    Accepts either a single snapshot (legacy callers) or a list of frames
    from one event. Multi-frame inputs are quality-weighted before priors.

    Falls back to vanilla matching if there's only one pet (no ranking
    to do) or no prior history (priors all return 0 → identical result).
    """
    now = now or time.time()
    if len(pets) <= 1:
        return match_against_pets(snapshot_bytes, pets)

    vec, frames_used, frames_offered, err = _embed_one_or_many(snapshot_bytes)
    if vec is None:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason=err,
                           frames_used=frames_used, frames_offered=frames_offered)

    # Aggregate cosine per pet (max across that pet's reference photos),
    # then layer the cloud-model boost on top per pet. The boost is
    # capped at ±CLOUD_BOOST_CAP so a strong cosine match can never get
    # flipped, but on close calls the per-pet calibration helps.
    # Filter foreign-backbone embeddings — see match_against_pets for
    # the rationale.
    active = embeddings.active_backbone_name()
    per_pet_cos: dict[str, tuple[str, float]] = {}
    for p in pets:
        best = -1.0
        for ph in p.photos:
            if (len(ph.embedding) != embeddings.EMBEDDING_DIM
                    or ph.backbone != active):
                continue
            v = np.asarray(ph.embedding, dtype=np.float32)
            sim = float(v @ vec)
            if sim > best:
                best = sim
        if best > -1.0:
            adjusted = best + _cloud_boost(p.pet_id, vec)
            per_pet_cos[p.pet_id] = (p.name, adjusted)

    if not per_pet_cos:
        return MatchResult(pet_id=None, pet_name=None, score=0.0,
                           confidence="unknown", reason="no reference embeddings",
                           frames_used=frames_used, frames_offered=frames_offered)

    cosines = [(pid, name, cos) for pid, (name, cos) in per_pet_cos.items()]
    hour = time.localtime(now).tm_hour
    ranked = _apply_priors(cosines, camera=camera, hour=hour, now=now,
                           bbox_area=bbox_area)
    pet_id, pet_name, cos_only, boosted = ranked[0]

    # Per-pet calibrated threshold (from finetune.calibrate_pet) trumps
    # the global one. The multi-pet path is exactly where calibration
    # matters most — two black cats with similar embeddings rely on
    # the tightened threshold to avoid swap errors.
    pet_threshold = MATCH_THRESHOLD
    for p in pets:
        if p.pet_id == pet_id and p.match_threshold > 0:
            pet_threshold = float(p.match_threshold)
            break

    if boosted >= HIGH_CONFIDENCE:
        confidence = "high"
    elif boosted >= pet_threshold:
        confidence = "tentative"
    else:
        return MatchResult(pet_id=None, pet_name=None, score=boosted,
                           confidence="unknown",
                           reason=f"top boosted score {boosted:.3f} below {pet_threshold}",
                           cosine_only=cos_only, prior_boost=boosted - cos_only,
                           frames_used=frames_used, frames_offered=frames_offered)

    return MatchResult(
        pet_id=pet_id, pet_name=pet_name,
        score=boosted, confidence=confidence,
        cosine_only=cos_only, prior_boost=boosted - cos_only,
        frames_used=frames_used, frames_offered=frames_offered,
    )


# ---- glue: identify a Frigate event ------------------------------------

def identify_event(snapshot_bytes: bytes | list[bytes], *,
                   event_id: str, camera: str,
                   label: str, start_time: float, end_time: float = 0.0,
                   bbox: Optional[tuple[float, float, float, float]] = None,
                   pet_store: PetStore | None = None) -> MatchResult:
    """One call from the Frigate event poller: embed, match, log, return.

    The Frigate poller can pass either a single snapshot (legacy path,
    still works) or a list of multiple frames sampled from the same event
    — the embedding layer pools them with per-frame quality weights, which
    materially improves accuracy on quick / motion-blurred events.

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
        frames_used=max(1, int(result.frames_used or 1)),
    )
    try:
        append_sighting(sighting)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to log sighting %s: %s", event_id, exc)
    return result
