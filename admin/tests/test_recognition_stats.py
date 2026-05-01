"""Tests for the recognition diagnostics page builder.

We seed the sightings log with a known mix of (pet_id, score, frames_used,
confidence) tuples and assert the rollup is what we expected. The page
template is excluded — we test the data shape (the only thing the
template can render) rather than the HTML.
"""
from __future__ import annotations

import json
import time

import yaml


def _seed_pets():
    from app import pets_store
    pets_store.PETS_YAML.parent.mkdir(parents=True, exist_ok=True)
    pets_store.PETS_YAML.write_text(yaml.safe_dump({
        "pets": [
            {"pet_id": "mochi", "name": "Mochi", "species": "cat",
             "match_threshold": 0.0, "photos": []},
            {"pet_id": "maru",  "name": "Maru",  "species": "cat",
             "match_threshold": 0.0, "photos": []},
        ],
    }, sort_keys=False))


def _seed_sightings(rows: list[dict]) -> None:
    from app import recognition
    recognition.SIGHTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with recognition.SIGHTINGS_LOG.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _row(*, ts: float, pet_id: str | None, score: float,
          confidence: str = "high", frames_used: int = 1) -> dict:
    out = {
        "event_id": f"e-{pet_id}-{ts}",
        "camera": "cam1",
        "pet_id": pet_id,
        "pet_name": pet_id.title() if pet_id else None,
        "score": score,
        "confidence": confidence,
        "start_time": ts,
        "end_time": ts + 1,
    }
    if frames_used > 1:
        out["frames_used"] = frames_used
    return out


def test_build_returns_empty_when_no_pets(data_dir):
    from app import recognition_stats
    diag = recognition_stats.build()
    assert diag.total_sightings == 0
    assert diag.pets == []


def test_build_aggregates_by_pet(data_dir):
    from app import recognition_stats

    _seed_pets()
    base = time.time()
    rows = [
        _row(ts=base - 100, pet_id="mochi", score=0.92),
        _row(ts=base - 200, pet_id="mochi", score=0.81, confidence="tentative"),
        _row(ts=base - 300, pet_id="mochi", score=0.95, frames_used=2),
        _row(ts=base - 400, pet_id="maru",  score=0.88),
        # Unknown — pet_id None counts toward unknown_share.
        _row(ts=base - 500, pet_id=None,    score=0.65, confidence="unknown"),
    ]
    _seed_sightings(rows)

    diag = recognition_stats.build()
    assert diag.total_sightings == 5
    by_id = {p.pet_id: p for p in diag.pets}
    assert by_id["mochi"].sightings == 3
    assert by_id["mochi"].high_confidence == 2     # 0.92 + 0.95 (high)
    assert by_id["mochi"].tentative == 1
    assert by_id["mochi"].multi_frame_count == 1
    assert by_id["maru"].sightings == 1
    # Unknown share = 1/5 = 0.2
    assert abs(diag.unknown_share - 0.2) < 1e-6
    assert diag.multi_frame_total == 1


def test_score_histogram_bins_correctly(data_dir):
    from app import recognition_stats
    _seed_pets()
    base = time.time()
    # Three rows landing in three different bins.
    _seed_sightings([
        _row(ts=base - 10, pet_id="mochi", score=0.55),  # 0.5-0.6
        _row(ts=base - 20, pet_id="mochi", score=0.81),  # 0.8-0.85
        _row(ts=base - 30, pet_id="mochi", score=0.97),  # 0.95-1.01
    ])
    diag = recognition_stats.build()
    mochi = next(p for p in diag.pets if p.pet_id == "mochi")
    # bins = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.01]
    # 0.55 -> bin 0; 0.81 -> bin 4; 0.97 -> bin 7
    assert mochi.score_histogram[0] == 1
    assert mochi.score_histogram[4] == 1
    assert mochi.score_histogram[7] == 1
    assert sum(mochi.score_histogram) == 3


def test_skips_sightings_for_deleted_pets(data_dir):
    """A sighting referencing a pet that's no longer in pets.yml shouldn't
    add a phantom PetStats row — it just gets dropped."""
    from app import recognition_stats
    _seed_pets()    # mochi + maru
    _seed_sightings([
        _row(ts=time.time(), pet_id="ghost", score=0.9),
    ])
    diag = recognition_stats.build()
    assert diag.total_sightings == 1
    assert all(p.pet_id != "ghost" for p in diag.pets)
