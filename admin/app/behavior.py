"""Bbox-based behavior signals for pet sightings.

We don't ship a pose-keypoint model (open pet-pose checkpoints aren't
in a state where we'd trust them on real customer footage; see
``docs/HUMAN_WORK.md`` for the deferred plan). What we *can* derive
without any new model are coarse activity classes from the bbox stream
that recognition already persists into ``sightings.ndjson``:

  * **resting** — long sustained presence with little bbox motion. The
    pet is sitting / sleeping / loafing in one spot.
  * **pacing** — repeated short visits to the same camera with small
    gaps. The pet is moving back and forth (often near a closed door
    or feeding area at meal time).
  * **active** — bbox area / position changing rapidly across nearby
    frames. The pet is playing, running, or in some other high-energy
    state.
  * **idle** — none of the above; not enough movement to classify but
    not sustained enough to be resting either. Default bucket.

These labels are useful to surface alongside the existing
"sightings count + heatmap" view: the same number of sightings can
mean very different things depending on whether the pet was *resting*
in one spot all day or *pacing* anxiously. Owners care about that
distinction; raw counts hide it.

What this module is NOT
-----------------------

These labels are *not* clinical signals. We deliberately do not name
behaviors like "vomiting" or "limping" — those need keypoint pose +
clinical signoff (see HUMAN_WORK.md). The four classes above are coarse
enough to be derivable from bbox alone and useful enough to ship now.

API
---

Three pure functions, all consumed by ``pet_health_overview``:

  * :func:`label_event` — turn one event's bbox stream into one of
    {resting, pacing, active, idle}. (For now we have only one bbox
    per event in sightings.ndjson, so this returns *idle* unless the
    aggregate stats below say otherwise.)
  * :func:`day_summary` — bucket today's sightings into label counts
    for a single pet. Surfaced as "today: 12 active, 8 resting, 3
    pacing" on /pets/health.
  * :func:`label_explanation` — plain-language one-liner per label
    used by the page copy.

License posture: stdlib + numpy. No new deps.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# A pacing burst is "≥ N visits to the same camera in W minutes, each
# separated by < gap_max seconds". The thresholds here are chosen so a
# cat that visits the food bowl 6 times in 10 minutes around dinner gets
# flagged as "pacing" but a single 5-minute play session doesn't.
PACING_MIN_VISITS = 4
PACING_WINDOW_SECONDS = 10 * 60
PACING_MAX_GAP_SECONDS = 90

# A resting cluster is "≥ N events on the same camera over ≥ T minutes
# with little bbox-area variance". Sleeping cats produce many short
# detections from the same spot; the variance check distinguishes
# "sleeping in one place" from "actively using one camera's view".
RESTING_MIN_EVENTS = 5
RESTING_MIN_DURATION = 10 * 60       # 10 min span minimum
RESTING_MAX_AREA_CV = 0.20           # coefficient of variation on area

# Active is the inverse — high bbox-area variation across rapid events.
ACTIVE_MIN_AREA_CV = 0.55
ACTIVE_MIN_EVENTS = 3
ACTIVE_WINDOW_SECONDS = 60


LABELS = ("resting", "pacing", "active", "idle")


@dataclass
class DaySummary:
    """One pet's label distribution for one local-day. Surfaced as
    chips on the /pets/health page."""
    pet_id: str
    pet_name: str
    date: str                                                 # local YYYY-MM-DD
    counts: dict[str, int] = field(default_factory=dict)      # label → events
    total_events: int = 0
    primary: str = "idle"                                     # most-frequent label

    def to_dict(self) -> dict:
        return {
            "pet_id": self.pet_id, "pet_name": self.pet_name,
            "date": self.date,
            "counts": dict(self.counts),
            "total_events": self.total_events,
            "primary": self.primary,
        }


def _bbox_area(bbox) -> float:
    """Pull bbox area from the persisted [x, y, w, h]. Returns 0 when
    the field is missing or malformed."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return 0.0
    try:
        w = float(bbox[2])
        h = float(bbox[3])
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, w) * max(0.0, h)


def _coefficient_of_variation(values: Sequence[float]) -> float:
    """std / mean, with safe handling of a zero mean. Used to ask
    "is the bbox area changing a lot?" — a cat that fills the same
    fraction of frame every event has CV near 0 (sleeping); a cat
    that's playing has CV that approaches 1."""
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean


def _label_cluster(events: list[dict]) -> str:
    """Single-camera cluster of recent events for one pet → label.

    The clustering is done at :func:`day_summary` level (per camera +
    contiguous-time block); this function just runs the rules on one
    cluster.
    """
    if len(events) < 2:
        return "idle"
    times = [float(e.get("start_time") or 0) for e in events]
    if not all(t > 0 for t in times):
        return "idle"
    times.sort()
    span = times[-1] - times[0]

    areas = [a for a in (_bbox_area(e.get("bbox")) for e in events) if a > 0]
    cv = _coefficient_of_variation(areas) if areas else 0.0

    # Pacing — many short-gap visits in a window.
    if len(events) >= PACING_MIN_VISITS and span <= PACING_WINDOW_SECONDS:
        gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        if gaps and max(gaps) <= PACING_MAX_GAP_SECONDS:
            return "pacing"

    # Active — sustained bbox-area change across a short window.
    if (len(events) >= ACTIVE_MIN_EVENTS
            and span <= ACTIVE_WINDOW_SECONDS
            and cv >= ACTIVE_MIN_AREA_CV):
        return "active"

    # Resting — long-span, low-variance area, lots of events.
    if (len(events) >= RESTING_MIN_EVENTS
            and span >= RESTING_MIN_DURATION
            and cv <= RESTING_MAX_AREA_CV):
        return "resting"

    return "idle"


def _split_into_clusters(events: list[dict],
                          *, max_gap: float = 300.0) -> list[list[dict]]:
    """Group events on the same camera into temporal clusters.

    Two events ``> max_gap`` seconds apart belong to different
    clusters. This is the unit each rule is evaluated against.

    The 5-minute default fits all three target behaviors: pacing
    bursts have <90 s gaps, active windows have <30 s gaps, and
    resting clusters tolerate 1-3 min gaps between barely-stirring
    detections. Tighter than this and resting falls apart into
    one-event-per-cluster idleness; looser and a quick visit blends
    into a long-ago cluster from the same camera.
    """
    if not events:
        return []
    sorted_events = sorted(events, key=lambda e: float(e.get("start_time") or 0))
    clusters: list[list[dict]] = [[sorted_events[0]]]
    for ev in sorted_events[1:]:
        prev_ts = float(clusters[-1][-1].get("start_time") or 0)
        ts = float(ev.get("start_time") or 0)
        if prev_ts <= 0 or ts <= 0:
            clusters[-1].append(ev)
            continue
        if ts - prev_ts <= max_gap:
            clusters[-1].append(ev)
        else:
            clusters.append([ev])
    return clusters


def label_events(events: Iterable[dict]) -> list[str]:
    """Per-event labels — output preserves *input* order.

    Each event's label is the label of the cluster (per-camera) it
    belongs to. Clustering happens internally on a chronological copy,
    but the returned list mirrors the caller's input order so they can
    pair `events[i]` with `labels[i]` directly without re-sorting.
    """
    rows = list(events)
    if not rows:
        return []
    # Cluster per camera, on a per-camera-chronological view, so a pet
    # ping-ponging between two cameras (e.g. living room ↔ kitchen at
    # meal time) doesn't get its within-camera resting cluster mixed up
    # with the other camera.
    by_cam: dict[str, list[dict]] = {}
    for ev in rows:
        cam = str(ev.get("camera") or "")
        by_cam.setdefault(cam, []).append(ev)
    label_by_event_id: dict[str, str] = {}
    for cam_events in by_cam.values():
        cam_sorted = sorted(cam_events,
                             key=lambda e: float(e.get("start_time") or 0))
        for cluster in _split_into_clusters(cam_sorted):
            label = _label_cluster(cluster)
            for ev in cluster:
                eid = str(ev.get("event_id") or id(ev))
                label_by_event_id[eid] = label
    return [
        label_by_event_id.get(str(ev.get("event_id") or id(ev)), "idle")
        for ev in rows
    ]


def day_summary(pet_id: str, pet_name: str, *,
                 events: Iterable[dict], now: float | None = None) -> DaySummary:
    """One day of behavior labels for one pet.

    ``events`` is the slice of ``sightings.ndjson`` already filtered
    by pet_id (caller does this — they typically already loaded
    sightings for the activity chart). Defensive about pet_id mismatch
    anyway.
    """
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    summary = DaySummary(pet_id=pet_id, pet_name=pet_name, date=today,
                          counts={k: 0 for k in LABELS})
    today_events = [
        e for e in events
        if e.get("pet_id") == pet_id
        and time.strftime("%Y-%m-%d",
                            time.localtime(float(e.get("start_time") or 0))) == today
    ]
    if not today_events:
        return summary
    labels = label_events(today_events)
    for lab in labels:
        summary.counts[lab] = summary.counts.get(lab, 0) + 1
    summary.total_events = len(labels)
    if summary.total_events > 0:
        # Primary = most-frequent non-idle label, fall through to idle
        # only when every event is idle. This makes the badge useful
        # ("today: mostly resting") even when there's a long tail of
        # idle events alongside a clear behavior pattern.
        non_idle = {k: v for k, v in summary.counts.items()
                     if k != "idle" and v > 0}
        if non_idle:
            summary.primary = max(non_idle.items(), key=lambda kv: kv[1])[0]
        else:
            summary.primary = "idle"
    return summary


def label_explanation(label: str, count: int, *, lang: str = "en") -> str:
    """Plain-language description for the /pets/health chip.

    Owner-friendly copy — no jargon. Translation strings live in
    ``i18n.py`` keyed ``BEHAVIOR_<LABEL>_EXPLANATION`` with a ``{n}``
    placeholder so non-English locales render the same idea natively.
    """
    if count <= 0 or label not in ("resting", "pacing", "active"):
        return ""
    from . import i18n
    key = f"BEHAVIOR_{label.upper()}_EXPLANATION"
    template = i18n.t(key, lang=lang)
    if not template or template == key:
        # Fallback when the i18n table doesn't have the key — keeps
        # tests that don't import i18n green.
        defaults = {
            "resting": f"Mostly resting today ({count} events).",
            "pacing":  f"Pacing back and forth detected ({count} events).",
            "active":  f"Lots of active movement today ({count} events).",
        }
        return defaults[label]
    return template.replace("{n}", str(count))
