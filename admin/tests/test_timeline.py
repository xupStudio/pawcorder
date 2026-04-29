"""Tests for the cross-camera journey stitcher."""
from __future__ import annotations

import time


def _seed_sightings(pet_id, points):
    """Each `points` entry is (camera, start, end, confidence)."""
    from app import recognition
    for cam, s, e, conf in points:
        recognition.append_sighting(recognition.Sighting(
            event_id=f"evt_{cam}_{s}", camera=cam, label="cat",
            pet_id=pet_id, pet_name=pet_id.title(),
            score=0.9 if conf == "high" else 0.8,
            confidence=conf, start_time=s, end_time=e,
        ))


def test_journeys_for_pet_groups_close_events(data_dir):
    from app import timeline
    now = time.time()
    _seed_sightings("mochi", [
        ("living", now - 600, now - 580, "high"),
        ("hallway", now - 575, now - 565, "high"),  # 5s gap → same journey
        ("kitchen", now - 560, now - 540, "high"),  # 5s gap → same journey
    ])
    journeys = timeline.journeys_for_pet("mochi")
    assert len(journeys) == 1
    assert [l.camera for l in journeys[0].legs] == ["living", "hallway", "kitchen"]


def test_journeys_split_on_long_gap(data_dir):
    from app import timeline
    now = time.time()
    _seed_sightings("mochi", [
        ("living", now - 600, now - 580, "high"),
        ("kitchen", now - 100, now - 80, "high"),  # 480s gap → separate journey
    ])
    journeys = timeline.journeys_for_pet("mochi", stitch_gap=90.0)
    assert len(journeys) == 2


def test_journeys_newest_first(data_dir):
    from app import timeline
    now = time.time()
    _seed_sightings("mochi", [
        ("a", now - 1000, now - 990, "high"),
        ("a", now - 100,  now - 90,  "high"),
    ])
    journeys = timeline.journeys_for_pet("mochi", stitch_gap=10.0)
    assert journeys[0].start_time > journeys[1].start_time


def test_journeys_excludes_other_pets(data_dir):
    from app import timeline
    now = time.time()
    _seed_sightings("mochi", [("a", now - 100, now - 90, "high")])
    _seed_sightings("maru", [("a", now - 100, now - 90, "high")])
    j_mochi = timeline.journeys_for_pet("mochi")
    j_maru = timeline.journeys_for_pet("maru")
    assert len(j_mochi) == 1 and len(j_maru) == 1
    assert j_mochi[0].pet_id == "mochi"
    assert j_maru[0].pet_id == "maru"


def test_journeys_empty_for_unknown_pet(data_dir):
    from app import timeline
    assert timeline.journeys_for_pet("ghost") == []


def test_journeys_includes_tentative_legs(data_dir):
    """Tentative-confidence legs are kept (UI dims them) but unknown
    sightings — those without a pet_id — never enter the timeline."""
    from app import timeline
    now = time.time()
    _seed_sightings("mochi", [
        ("a", now - 100, now - 90, "high"),
        ("a", now - 80,  now - 70, "tentative"),
    ])
    journeys = timeline.journeys_for_pet("mochi", stitch_gap=30.0)
    assert len(journeys) == 1
    confidences = [l.confidence for l in journeys[0].legs]
    assert "tentative" in confidences


def test_cross_camera_summary_aggregates(data_dir):
    from app import timeline
    now = time.time()
    _seed_sightings("mochi", [
        ("kitchen", now - 600, now - 590, "high"),
        ("living",  now - 200, now - 190, "high"),
    ])
    _seed_sightings("maru", [("garage", now - 300, now - 290, "high")])
    summary = timeline.cross_camera_summary(since_hours=1.0)
    assert "mochi" in summary
    assert summary["mochi"]["sightings"] == 2
    assert sorted(summary["mochi"]["cameras"]) == ["kitchen", "living"]
    assert summary["maru"]["sightings"] == 1


def test_cross_camera_summary_skips_old_events(data_dir):
    from app import timeline
    now = time.time()
    _seed_sightings("mochi", [("a", now - 40 * 3600, now - 40 * 3600 + 5, "high")])
    summary = timeline.cross_camera_summary(since_hours=1.0)
    assert "mochi" not in summary
