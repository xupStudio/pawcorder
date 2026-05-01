"""Bbox-based behavior labels — pure-function tests.

We don't load sightings.ndjson here; behavior.py operates on plain
dicts in the shape `recognition.read_sightings` returns, so the tests
build them inline. That keeps the math under test instead of the
plumbing.
"""
from __future__ import annotations

import time

from app import behavior


def _ev(*, ts: float, camera: str = "cam1", pet_id: str = "mochi",
         bbox=(0.4, 0.4, 0.2, 0.2), event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or f"e-{ts}",
        "camera": camera,
        "pet_id": pet_id,
        "start_time": ts,
        "bbox": list(bbox),
    }


def test_resting_label_for_long_low_variance_cluster():
    """5 events spaced over 12 minutes on the same camera with stable
    bbox area → resting."""
    base = time.time()
    events = [
        _ev(ts=base + i * 180, bbox=(0.4, 0.4, 0.2, 0.2))  # 3 min gaps
        for i in range(5)
    ]
    labels = behavior.label_events(events)
    assert all(l == "resting" for l in labels), labels


def test_pacing_label_for_rapid_short_visits():
    """4+ visits in <10 min, gaps under 90 s → pacing."""
    base = time.time()
    events = [_ev(ts=base + i * 60) for i in range(4)]   # 60s gaps, 3 min span
    labels = behavior.label_events(events)
    assert all(l == "pacing" for l in labels), labels


def test_active_label_for_high_variance_short_window():
    """3+ events in 60 s with varying bbox area → active."""
    base = time.time()
    events = [
        _ev(ts=base, bbox=(0.1, 0.1, 0.10, 0.10)),
        _ev(ts=base + 10, bbox=(0.5, 0.5, 0.40, 0.40)),
        _ev(ts=base + 25, bbox=(0.2, 0.2, 0.18, 0.18)),
    ]
    labels = behavior.label_events(events)
    assert all(l == "active" for l in labels), labels


def test_idle_when_no_pattern_matches():
    """Two events spaced 5 min apart, similar bbox — neither pacing,
    resting, nor active → idle."""
    base = time.time()
    events = [_ev(ts=base), _ev(ts=base + 300)]
    labels = behavior.label_events(events)
    assert labels == ["idle", "idle"]


def test_per_camera_clustering_keeps_labels_separate():
    """A pet that ping-pongs between two cameras shouldn't get one
    cluster's resting span confused with the other's pacing burst.
    Distinct event_ids per camera so the lookup table doesn't collide."""
    base = time.time()
    cam1_events = [
        _ev(ts=base + i * 200, camera="cam1", event_id=f"c1-{i}")
        for i in range(5)
    ]                                                                 # resting
    cam2_events = [
        _ev(ts=base + i * 60, camera="cam2", event_id=f"c2-{i}")
        for i in range(4)
    ]                                                                 # pacing
    labels_by_id = dict(zip(
        [e["event_id"] for e in cam1_events + cam2_events],
        behavior.label_events(cam1_events + cam2_events),
    ))
    for ev in cam1_events:
        assert labels_by_id[ev["event_id"]] == "resting"
    for ev in cam2_events:
        assert labels_by_id[ev["event_id"]] == "pacing"


def test_day_summary_picks_dominant_non_idle_label():
    """Mixed day with mostly idle but a resting cluster → primary='resting'."""
    base = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(base))
    # 5 resting + 2 idle (gap > 60s and not a pattern)
    events = [
        _ev(ts=base + i * 180) for i in range(5)
    ] + [
        _ev(ts=base + 10000, event_id="solo-1"),
        _ev(ts=base + 20000, event_id="solo-2"),
    ]
    summary = behavior.day_summary("mochi", "Mochi", events=events, now=base)
    assert summary.date == today
    assert summary.primary == "resting"
    assert summary.counts["resting"] == 5
    assert summary.total_events == 7


def test_day_summary_filters_by_pet_id():
    """Events for other pets must not contribute."""
    base = time.time()
    events = [
        _ev(ts=base + i * 60, pet_id="mochi") for i in range(4)
    ] + [
        _ev(ts=base + i * 60, pet_id="maru") for i in range(4)
    ]
    summary = behavior.day_summary("mochi", "Mochi", events=events, now=base)
    assert summary.total_events == 4
    assert summary.primary == "pacing"


def test_day_summary_empty_when_nothing_today():
    """Events from yesterday don't count toward today's primary label."""
    base = time.time()
    yesterday = base - 86400 * 2
    events = [_ev(ts=yesterday + i * 60) for i in range(4)]
    summary = behavior.day_summary("mochi", "Mochi", events=events, now=base)
    assert summary.total_events == 0
    assert summary.primary == "idle"
    # All defined labels should be present in the counts dict, all 0.
    assert summary.counts == {k: 0 for k in behavior.LABELS}


def test_label_explanation_quiet_for_idle():
    """Idle is the default — we don't surface a chip for it."""
    assert behavior.label_explanation("idle", 5) == ""


def test_label_explanation_includes_count():
    msg = behavior.label_explanation("resting", 12)
    assert "resting" in msg.lower()
    assert "12" in msg
