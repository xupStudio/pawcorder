"""Pose-keypoint scaffolding for future behavior models.

Where this fits
---------------

The current behavior pipeline (``app.behavior``) labels events from
the bbox stream — resting / pacing / active / eating / drinking. Going
beyond that to clinical signals (limping / scratching / vomiting)
needs **per-frame keypoints**, not just a bounding box.

This module is the scaffolding for that next step. It defines:

  * The :class:`PoseExtractor` interface a real implementation will
    fill in (load an ONNX pose model, run on a snapshot, return
    keypoints in normalised 0..1 coords).
  * A :data:`KEYPOINTS` schema covering the 17 joints typical of
    YOLO-pose / RTMPose / MoveNet outputs — head, ears, paws, hips,
    spine — so any future model can map onto a stable shape.
  * A no-op default implementation that returns "not available" so
    callers can soft-fail instead of crashing when no pose model is
    deployed.
  * A :func:`classify_from_keypoints` shell with rule signatures the
    *human* design step in HUMAN_WORK.md will fill in once we have a
    pose model + sample footage to calibrate against.

What we deliberately do NOT ship
--------------------------------

Real pose model weights. The candidates (YOLOv8-pose, RTMPose-T,
MoveNet) are all human-pose-trained — running them on cats / dogs
gives unreliable keypoints. Picking one (plus optional fine-tuning
on AP-10K or a similar quadruped dataset) is the human-validation
step tracked in `docs/HUMAN_WORK.md` under "Pose-based behavior
detection".

When that lands, swap the no-op implementation for a real ONNX
extractor and the rule classifier — :func:`classify_from_keypoints`'s
caller surface won't change.

License posture: stdlib only at scaffold level. The future ONNX
runner will add onnxruntime (already an admin transitive dep via
embeddings.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence


# 17-keypoint schema — same indexing as YOLOv8-pose / RTMPose / MoveNet
# for human pose. We re-purpose for quadrupeds where it overlaps
# (head / shoulders / hips); the leg points map to "paws" instead of
# wrists / ankles. A future pet-pose-trained model can either ship
# this schema or define its own — the code below gates lookups on
# named constants so a new schema would be a small remap, not a
# whole-pipeline change.
KEYPOINTS = (
    "nose",
    "left_eye", "right_eye",
    "left_ear", "right_ear",
    "left_shoulder", "right_shoulder",
    "left_elbow",    "right_elbow",     # forepaws (front knees)
    "left_wrist",    "right_wrist",     # forepaw tips
    "left_hip",      "right_hip",
    "left_knee",     "right_knee",      # hindpaws (back knees)
    "left_ankle",    "right_ankle",     # hindpaw tips
)


@dataclass
class Keypoint:
    """One detected joint. Coords are normalised to the source frame's
    width / height so the same number is meaningful across cameras."""
    name: str
    x: float                  # 0.0 = left edge, 1.0 = right
    y: float                  # 0.0 = top edge, 1.0 = bottom
    confidence: float = 0.0   # 0.0 = no detection, 1.0 = certain


@dataclass
class PoseResult:
    """All 17 keypoints for one frame, plus a top-level confidence
    summary so downstream code can skip noisy detections cheaply."""
    keypoints: list[Keypoint] = field(default_factory=list)
    overall_confidence: float = 0.0
    success: bool = False
    error: str = ""

    def by_name(self) -> dict[str, Keypoint]:
        """Quick lookup. Empty when ``success`` is False."""
        return {kp.name: kp for kp in self.keypoints}


class PoseExtractor:
    """Interface a real pose model will satisfy.

    ``load()`` is allowed to be lazy + soft-fail (mirrors the embedding
    extractor's contract). ``extract(image_bytes)`` returns a PoseResult
    with ``success=False`` whenever the model isn't loaded — callers
    then fall through to bbox-only behavior labels.
    """

    name: str = "noop"

    def load(self) -> bool:
        return False

    def extract(self, image_bytes: bytes) -> PoseResult:
        return PoseResult(success=False, error="pose extractor not configured")


# Module-level singleton so the same extractor is reused. Tests
# override via :func:`set_extractor`.
_extractor: PoseExtractor = PoseExtractor()


def get_extractor() -> PoseExtractor:
    return _extractor


def set_extractor(ext: PoseExtractor | None) -> None:
    """Inject a custom extractor (production: real ONNX model;
    tests: a stub returning canned keypoints). Pass ``None`` to
    reset."""
    global _extractor
    _extractor = ext if ext is not None else PoseExtractor()


# ---- behavior classification skeleton --------------------------------
#
# Concrete rule library lives here when we have a real pose model + a
# sample of customer footage. Each rule is a callable mapping a
# PoseResult to (label, confidence) — the dispatcher picks the highest-
# confidence non-None result, falling back to None when nothing fires.
#
# Examples we know we want once the model lands (placeholders only):
#   * SCRATCHING — head down, one hindpaw raised + oscillating
#   * LIMPING    — asymmetric weight distribution across forepaws
#   * GROOMING   — head bent toward shoulder/flank, low overall motion
# These need real footage to calibrate; HUMAN_WORK.md tracks them.

_RULES: list = []     # populate when pose model is wired


def classify_from_keypoints(
        results: Sequence[PoseResult],
) -> tuple[str | None, float]:
    """Dispatch a sequence of frame-level PoseResults through the
    registered rules. Returns ``(label, confidence)`` for the highest-
    confidence rule that fired, or ``(None, 0.0)`` if no rule matches
    or the pose model wasn't available. Designed to be called from a
    behavior-aggregation pass; always returns cleanly so the caller
    can soft-fail.
    """
    if not results or not _RULES:
        return None, 0.0
    # Real implementation:
    #   best_label, best_conf = None, 0.0
    #   for rule in _RULES:
    #       lab, conf = rule(results)
    #       if lab and conf > best_conf:
    #           best_label, best_conf = lab, conf
    #   return best_label, best_conf
    return None, 0.0


def is_available() -> bool:
    """Public surface used by the /pets/health page to decide whether
    to render a "pose-based behavior" section. Returns True only when
    a real (non-no-op) extractor is loaded *and* at least one rule is
    registered. Both gates today return False — this is by design."""
    return _extractor.name != "noop" and bool(_RULES) and _extractor.load()
