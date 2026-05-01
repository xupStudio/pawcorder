"""Read-only stats page that surfaces what the recognition pipeline
is *actually* doing on production sightings.

Why this page exists
--------------------

Owners after the AI batches reasonably ask "is the new
multi-frame matching / cloud-trained classifier / DINOv2 backbone
doing anything for me?". Without surfacing concrete numbers the
upgrades feel like a marketing claim. This module aggregates the
sightings log into:

  * **Score histogram per pet** — distribution of cosine + boost
    scores. A confident pet's distribution clusters above 0.85; a
    miscalibrated one shows a bimodal mess at the threshold.
  * **Multi-frame coverage** — share of today's events that fed >1
    frame to the matcher. The infra is wired (telegram fetches
    snapshot + thumbnail) so the rate should be high; if it isn't,
    Frigate's snapshot endpoint is failing.
  * **Cloud-boost effect** — for pets with a V2 cloud model, how
    often does the boost flip the predicted pet? Capped at ±0.08 by
    design, so flips are rare; a count of 0 over a week suggests
    the model isn't pulling its weight.
  * **Confidence mix** — high / tentative / unknown share over the
    last N days, week-over-week.

License posture: pure-stdlib + numpy. Reads ``sightings.ndjson``
directly via :func:`recognition.read_sightings`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


# Default lookback. 14 days is enough that every pet has *some* data
# in a typical multi-pet household; longer makes the score histograms
# harder to interpret (scores can drift after a backbone swap).
DEFAULT_LOOKBACK_DAYS = 14

# Score histogram bin edges. Tuned against the operational range:
# below 0.5 is essentially a non-match (recognition.py rejects under
# MATCH_THRESHOLD=0.78 anyway), so we don't waste bins on the bottom
# 60% of the unit interval.
_SCORE_BINS = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.01]


@dataclass
class PetStats:
    """One pet's recognition stats over the lookback window."""
    pet_id: str
    pet_name: str
    sightings: int = 0
    score_histogram: list[int] = field(default_factory=list)
    score_bin_edges: list[float] = field(default_factory=list)
    high_confidence: int = 0
    tentative: int = 0
    multi_frame_count: int = 0           # events with frames_used > 1
    cloud_boost_active: bool = False     # V2 cloud model present + loaded

    def to_dict(self) -> dict:
        return {
            "pet_id": self.pet_id, "pet_name": self.pet_name,
            "sightings": self.sightings,
            "score_histogram": self.score_histogram,
            "score_bin_edges": self.score_bin_edges,
            "high_confidence": self.high_confidence,
            "tentative": self.tentative,
            "multi_frame_count": self.multi_frame_count,
            "cloud_boost_active": self.cloud_boost_active,
        }


@dataclass
class RecognitionDiagnostics:
    """Page-level rollup. Pets list + cross-pet aggregate stats."""
    days: int
    total_sightings: int
    pets: list[PetStats] = field(default_factory=list)
    multi_frame_total: int = 0
    multi_frame_share: float = 0.0       # 0..1, share of events with >1 frame
    high_confidence_share: float = 0.0   # 0..1
    unknown_share: float = 0.0           # 0..1, "no pet matched"
    active_backbone: str = ""
    backbone_display: str = ""           # plain-language label

    def to_dict(self) -> dict:
        return {
            "days": self.days,
            "total_sightings": self.total_sightings,
            "pets": [p.to_dict() for p in self.pets],
            "multi_frame_total": self.multi_frame_total,
            "multi_frame_share": round(self.multi_frame_share, 4),
            "high_confidence_share": round(self.high_confidence_share, 4),
            "unknown_share": round(self.unknown_share, 4),
            "active_backbone": self.active_backbone,
            "backbone_display": self.backbone_display,
        }


def _bin_index(score: float) -> int:
    """Return the bucket index for a score in :data:`_SCORE_BINS`.
    Scores below the first edge bucket to 0; scores above the last
    bucket to ``len(bins)-2`` (the open-ended top bin)."""
    for i in range(len(_SCORE_BINS) - 1):
        if score < _SCORE_BINS[i + 1]:
            return i
    return len(_SCORE_BINS) - 2


def build(*, days: int = DEFAULT_LOOKBACK_DAYS,
            now: Optional[float] = None) -> RecognitionDiagnostics:
    """Aggregate sightings.ndjson into the diagnostics shape.

    Pure read — no side effects, no network. Safe to call from a Web
    handler synchronously even on a busy install (10K rows × few
    µs each is well under a frame budget).
    """
    from . import embeddings, recognition
    from .pets_store import PetStore

    now = now or time.time()
    cutoff = now - days * 86400
    rows = recognition.read_sightings(limit=50_000, since=cutoff)
    pets = {p.pet_id: p for p in PetStore().load()}

    # Per-pet aggregation
    by_pet: dict[str, PetStats] = {}
    for pid, pet in pets.items():
        ps = PetStats(
            pet_id=pid, pet_name=pet.name,
            score_histogram=[0] * (len(_SCORE_BINS) - 1),
            score_bin_edges=list(_SCORE_BINS),
            cloud_boost_active=recognition.has_cloud_model(pid)
                and recognition._load_cloud_model(pid) is not None,
        )
        by_pet[pid] = ps

    multi_total = 0
    high_total = 0
    unknown_total = 0
    for r in rows:
        pid = r.get("pet_id")
        score = float(r.get("score") or 0.0)
        conf = r.get("confidence") or "unknown"
        frames = int(r.get("frames_used") or 1)
        if frames > 1:
            multi_total += 1
        if conf == "high":
            high_total += 1
        if pid is None:
            unknown_total += 1
            continue
        ps = by_pet.get(pid)
        if ps is None:
            # Sighting recorded against a pet that was deleted later.
            # Skip — it would skew per-pet stats and the operator
            # already can't act on a removed pet.
            continue
        ps.sightings += 1
        if score > 0:
            ps.score_histogram[_bin_index(score)] += 1
        if conf == "high":
            ps.high_confidence += 1
        elif conf == "tentative":
            ps.tentative += 1
        if frames > 1:
            ps.multi_frame_count += 1

    n = len(rows)
    backbone = embeddings.active_backbone_name()
    # Map registry name → owner-friendly label. Anything unmapped (a
    # future-added backbone, a typo'd env var) gets a generic
    # "Custom model" — never the raw key, since this string lands
    # directly on /recognition for pet owners to read.
    backbone_display_keys = {
        "mobilenetv3_small_100": "Fast (default)",
        "dinov2_small": "Accurate",
    }
    return RecognitionDiagnostics(
        days=days,
        total_sightings=n,
        pets=sorted(by_pet.values(), key=lambda p: -p.sightings),
        multi_frame_total=multi_total,
        multi_frame_share=multi_total / n if n else 0.0,
        high_confidence_share=high_total / n if n else 0.0,
        unknown_share=unknown_total / n if n else 0.0,
        active_backbone=backbone,
        backbone_display=backbone_display_keys.get(backbone, "Custom model"),
    )
