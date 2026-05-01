"""Tests for the shared `recognition.daily_buckets` helper.

Several health surfaces (pet_health_overview, vet_pack, bowl_monitor,
litter_monitor) compose this helper to bucket sightings by local-day.
A regression here would silently drift every chart out of agreement
with the live alerter.
"""
from __future__ import annotations

import time


def _row(*, ts: float, pet_id: str = "mochi", camera: str = "kitchen") -> dict:
    return {
        "event_id": f"e-{ts}", "camera": camera, "label": "cat",
        "pet_id": pet_id, "pet_name": pet_id.title(),
        "score": 0.9, "confidence": "high",
        "start_time": ts, "end_time": ts + 1,
    }


def test_buckets_three_days(data_dir):
    from app import recognition
    today = time.strftime("%Y-%m-%d", time.localtime())
    midnight = time.mktime(time.strptime(today, "%Y-%m-%d"))
    rows = [
        _row(ts=midnight + 100),       # today × 1
        _row(ts=midnight - 86400 + 100),  # yesterday × 1
        _row(ts=midnight - 86400 + 200),  # yesterday × 2
    ]
    out = recognition.daily_buckets(rows, pet_id="mochi", now=midnight + 10000,
                                      days=3)
    # oldest first → [day-2, day-1, day-0]
    assert out == [0, 2, 1]


def test_buckets_filter_by_pet(data_dir):
    from app import recognition
    now = time.time()
    rows = [
        _row(ts=now - 100, pet_id="mochi"),
        _row(ts=now - 200, pet_id="maru"),
        _row(ts=now - 300, pet_id="mochi"),
    ]
    out = recognition.daily_buckets(rows, pet_id="mochi", now=now, days=1)
    assert out == [2]


def test_buckets_with_predicate(data_dir):
    """Predicate gates rows past the pet filter — used by litter / bowl."""
    from app import recognition
    now = time.time()
    rows = [
        _row(ts=now - 100, camera="kitchen"),
        _row(ts=now - 200, camera="kitchen"),
        _row(ts=now - 300, camera="hallway"),
    ]
    only_kitchen = lambda r: r.get("camera") == "kitchen"
    out = recognition.daily_buckets(
        rows, pet_id="mochi", predicate=only_kitchen, now=now, days=1,
    )
    assert out == [2]


def test_buckets_skips_zero_timestamp(data_dir):
    from app import recognition
    rows = [_row(ts=0), _row(ts=time.time() - 100)]
    out = recognition.daily_buckets(rows, pet_id="mochi",
                                     now=time.time(), days=1)
    assert out == [1]


def test_buckets_empty_window():
    from app import recognition
    out = recognition.daily_buckets([], pet_id="mochi",
                                     now=time.time(), days=7)
    assert out == [0] * 7


def test_buckets_zero_days_returns_empty():
    """Defensive: ``days=0`` should return [] rather than crash."""
    from app import recognition
    out = recognition.daily_buckets([], pet_id="mochi",
                                     now=time.time(), days=0)
    assert out == []
