"""Tests for recognition matching, sightings log, and event hook."""
from __future__ import annotations

import json

import numpy as np
import pytest


def _fake_extractor(target_vector):
    """Return an EmbeddingExtractor stand-in whose .extract() always
    returns the given vector. For controlled cosine-similarity tests."""
    from app.embeddings import EmbeddingResult

    class _Fake:
        def load(self): return True
        def extract(self, _bytes):
            v = np.asarray(target_vector, dtype=np.float32)
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
            return EmbeddingResult(vector=v, success=True)
    return _Fake()


def _ref_pet(name, embedding_vec):
    """Build a Pet with one reference photo embedding (already normalized)."""
    from app.pets_store import Pet, PetPhoto
    v = np.asarray(embedding_vec, dtype=np.float32)
    n = np.linalg.norm(v)
    if n > 0:
        v = v / n
    return Pet(
        pet_id=name.lower(),
        name=name,
        species="cat",
        photos=[PetPhoto(filename="ref.jpg", embedding=v.tolist())],
    )


def test_match_against_zero_pets_returns_unknown(data_dir):
    from app import recognition
    out = recognition.match_against_pets(b"x", [])
    assert out.pet_id is None
    assert out.confidence == "unknown"


def test_match_picks_high_confidence_when_close(data_dir, monkeypatch):
    from app import embeddings, recognition

    # Reference and query both lie in the same direction → cosine ≈ 1.0
    ref = [1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 1)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _fake_extractor(ref))

    pets = [_ref_pet("Mochi", ref)]
    out = recognition.match_against_pets(b"x", pets)
    assert out.pet_id == "mochi"
    assert out.confidence == "high"
    assert out.score >= 0.99


def test_match_picks_tentative_when_in_between(data_dir, monkeypatch):
    from app import embeddings, recognition

    # Reference and query roughly aligned but not identical:
    # cos(angle) tuned to land between MATCH_THRESHOLD and HIGH_CONFIDENCE.
    ref = [1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 1)
    query = [0.83] + [0.5] + [0.0] * (embeddings.EMBEDDING_DIM - 2)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _fake_extractor(query))

    pets = [_ref_pet("Mochi", ref)]
    out = recognition.match_against_pets(b"x", pets)
    assert out.pet_id == "mochi"
    assert out.confidence == "tentative"
    assert recognition.MATCH_THRESHOLD <= out.score < recognition.HIGH_CONFIDENCE


def test_match_unknown_when_below_threshold(data_dir, monkeypatch):
    from app import embeddings, recognition

    # Orthogonal vectors → cosine = 0
    ref = [1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 1)
    query = [0.0, 1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 2)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _fake_extractor(query))

    pets = [_ref_pet("Mochi", ref)]
    out = recognition.match_against_pets(b"x", pets)
    assert out.pet_id is None
    assert out.confidence == "unknown"


def test_match_picks_best_among_multiple_pets(data_dir, monkeypatch):
    from app import embeddings, recognition

    a_ref = [1.0, 0.0] + [0.0] * (embeddings.EMBEDDING_DIM - 2)
    b_ref = [0.0, 1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 2)
    # Query closer to b_ref.
    query = [0.0, 0.95, 0.31] + [0.0] * (embeddings.EMBEDDING_DIM - 3)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _fake_extractor(query))

    pets = [_ref_pet("Mochi", a_ref), _ref_pet("Maru", b_ref)]
    out = recognition.match_against_pets(b"x", pets)
    assert out.pet_id == "maru"


def test_match_handles_bad_embedding_dim(data_dir, monkeypatch):
    """A photo whose stored embedding length is wrong (corrupted YAML?)
    must be silently skipped, not crash the matcher."""
    from app import embeddings, recognition
    from app.pets_store import Pet, PetPhoto

    monkeypatch.setattr(embeddings, "get_extractor",
                        lambda: _fake_extractor([1.0] * embeddings.EMBEDDING_DIM))
    # Embedding intentionally too short.
    pet = Pet(pet_id="mochi", name="Mochi", species="cat",
              photos=[PetPhoto(filename="bad.jpg", embedding=[0.1, 0.2, 0.3])])
    out = recognition.match_against_pets(b"x", [pet])
    assert out.pet_id is None
    assert "no reference embeddings" in out.reason


def test_match_when_extractor_fails(data_dir, monkeypatch):
    from app import embeddings, recognition
    from app.embeddings import EmbeddingResult

    class _Failing:
        def load(self): return False
        def extract(self, _b):
            return EmbeddingResult(
                vector=np.zeros(embeddings.EMBEDDING_DIM, dtype=np.float32),
                success=False, error="model not loaded",
            )
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _Failing())
    out = recognition.match_against_pets(b"x", [_ref_pet("Mochi", [1.0])])
    assert out.pet_id is None
    assert "model not loaded" in out.reason


# ---- sightings log -----------------------------------------------------

def test_sighting_round_trip(data_dir):
    from app import recognition
    s = recognition.Sighting(
        event_id="evt1", camera="kitchen", label="cat",
        pet_id="mochi", pet_name="Mochi", score=0.91,
        confidence="high", start_time=1000.0, end_time=1010.0,
    )
    recognition.append_sighting(s)
    rows = recognition.read_sightings()
    assert len(rows) == 1
    assert rows[0]["pet_id"] == "mochi"
    assert rows[0]["score"] == 0.91


def test_read_sightings_filters_by_since(data_dir):
    from app import recognition
    for ts in (100.0, 200.0, 300.0):
        recognition.append_sighting(recognition.Sighting(
            event_id=f"e{ts}", camera="c", label="cat", pet_id="m", pet_name="M",
            score=0.9, confidence="high", start_time=ts, end_time=ts + 1,
        ))
    rows = recognition.read_sightings(since=150.0)
    assert {r["event_id"] for r in rows} == {"e200.0", "e300.0"}


def test_stats_for_pet(data_dir, monkeypatch):
    import time
    from app import recognition
    now = time.time()
    for i, pet in enumerate(["mochi", "mochi", "maru"]):
        recognition.append_sighting(recognition.Sighting(
            event_id=f"e{i}", camera="kitchen" if i % 2 == 0 else "living",
            label="cat", pet_id=pet, pet_name=pet.title(),
            score=0.9, confidence="high",
            start_time=now - 600 + i, end_time=now - 590 + i,
        ))
    s = recognition.stats_for_pet("mochi", since_hours=1.0)
    assert s["sightings"] == 2
    assert "kitchen" in s["cameras"]


def test_log_handles_corrupt_lines(data_dir):
    """A garbled line in the log must not crash read_sightings."""
    from app import recognition
    recognition.SIGHTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    recognition.SIGHTINGS_LOG.write_text(
        '{"event_id":"good","camera":"k","label":"cat","pet_id":"m","pet_name":"M",'
        '"score":0.9,"confidence":"high","start_time":100,"end_time":110}\n'
        'NOT_JSON_AT_ALL\n'
        '{"event_id":"good2","camera":"k","label":"cat","pet_id":"m","pet_name":"M",'
        '"score":0.8,"confidence":"high","start_time":200,"end_time":210}\n',
        encoding="utf-8",
    )
    rows = recognition.read_sightings()
    assert {r["event_id"] for r in rows} == {"good", "good2"}


def test_identify_event_writes_log_and_returns_match(data_dir, monkeypatch):
    from app import embeddings, recognition
    from app.pets_store import PetStore

    ref = [1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 1)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _fake_extractor(ref))
    store = PetStore()
    pet = store.create(name="Mochi", species="cat")
    store.add_photo(pet.pet_id, b"x", ref)

    out = recognition.identify_event(
        b"snapshot_bytes",
        event_id="evt-42", camera="kitchen", label="cat",
        start_time=1234.0, end_time=1240.0,
        pet_store=store,
    )
    assert out.pet_id == "mochi"
    rows = recognition.read_sightings()
    assert any(r["event_id"] == "evt-42" for r in rows)


# ---- multi-pet heuristic priors ---------------------------------------

def _seed_history(rows: list[dict]) -> None:
    """Write a list of confident-sighting rows into the log so the
    prior-builder has something to learn from."""
    from app import recognition
    recognition.SIGHTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with recognition.SIGHTINGS_LOG.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    # Reset module cache so the next call rebuilds fresh.
    recognition._prior_cache.by_pet = None  # type: ignore[attr-defined]
    recognition._prior_cache.built_at = 0.0  # type: ignore[attr-defined]


def _row(pet_id: str, *, hour: int, camera: str, ts: float = 1_700_000_000):
    """A confident-sighting row at the given hour-of-day."""
    import time
    base = time.mktime(time.strptime("2024-11-14 00:00", "%Y-%m-%d %H:%M"))
    return {
        "event_id": f"evt-{pet_id}-{hour}-{camera}",
        "camera": camera, "label": "cat",
        "pet_id": pet_id, "pet_name": pet_id.title(),
        "score": 0.92, "confidence": "high",
        "start_time": base + hour * 3600, "end_time": base + hour * 3600 + 5,
    }


def test_priors_zero_when_no_history(data_dir):
    """Empty sightings log → priors return 0 for every pet → boosted = cosine."""
    from app import recognition
    _seed_history([])
    cosines = [("mochi", "Mochi", 0.80), ("maru", "Maru", 0.78)]
    out = recognition._apply_priors(cosines, camera="front", hour=8, now=1_700_000_000)
    assert out[0][0] == "mochi"  # mochi still wins on cosine
    # Boosts all zero.
    for _, _, cos, boosted in out:
        assert cos == boosted


def test_time_prior_boosts_pet_seen_at_hour(data_dir):
    """Mochi has 3am history, Maru doesn't → at 3am, Mochi gets a boost."""
    from app import recognition
    rows = [_row("mochi", hour=3, camera="front") for _ in range(20)]
    rows += [_row("maru", hour=14, camera="front") for _ in range(20)]
    _seed_history(rows)

    priors = recognition._build_priors(now=1_700_000_000)
    p_mochi_3am = recognition._time_prior("mochi", 3, priors)
    p_maru_3am = recognition._time_prior("maru", 3, priors)
    assert p_mochi_3am > 0
    assert p_maru_3am < 0


def test_priors_can_flip_borderline_match(data_dir):
    """A borderline cosine tie (0.79 vs 0.80) must let priors flip the
    winner. Mochi is the bedroom cat; Maru is the kitchen cat. Event on
    bedroom camera should pick Mochi even when Maru's cosine is slightly
    higher."""
    from app import recognition
    rows = [_row("mochi", hour=2, camera="bedroom") for _ in range(20)]
    rows += [_row("maru", hour=14, camera="kitchen") for _ in range(20)]
    _seed_history(rows)

    cosines = [("maru", "Maru", 0.80), ("mochi", "Mochi", 0.79)]
    out = recognition._apply_priors(cosines, camera="bedroom",
                                     hour=2, now=1_700_000_000)
    assert out[0][0] == "mochi"


def test_priors_cannot_override_strong_cosine_lead(data_dir):
    """Even with Mochi's strong priors, Maru with cosine 0.95 vs Mochi's
    0.50 must still win. PRIOR_TOTAL_CAP is the safety belt."""
    from app import recognition
    rows = [_row("mochi", hour=2, camera="bedroom") for _ in range(50)]
    _seed_history(rows)

    cosines = [("maru", "Maru", 0.95), ("mochi", "Mochi", 0.50)]
    out = recognition._apply_priors(cosines, camera="bedroom",
                                     hour=2, now=1_700_000_000)
    assert out[0][0] == "maru"
    # Boost on Maru must be ≤ cap.
    boost_maru = next(b - c for pid, _, c, b in out if pid == "maru")
    assert abs(boost_maru) <= recognition.PRIOR_TOTAL_CAP + 1e-6


def test_inertia_prior_boosts_recent_same_camera(data_dir):
    """A pet seen on the same camera 30 seconds ago gets a +inertia boost
    on the next match. Across cameras → no boost."""
    from app import recognition
    recent = [{
        "event_id": "recent", "camera": "front", "label": "cat",
        "pet_id": "mochi", "pet_name": "Mochi", "score": 0.91,
        "confidence": "high", "start_time": 1000.0, "end_time": 1005.0,
    }]
    boost_same = recognition._inertia_prior("mochi", "front", recent_rows=recent)
    boost_other_cam = recognition._inertia_prior("mochi", "back", recent_rows=recent)
    boost_other_pet = recognition._inertia_prior("maru", "front", recent_rows=recent)
    assert boost_same == recognition.PRIOR_WEIGHT_INERTIA
    assert boost_other_cam == 0.0
    assert boost_other_pet == 0.0


def test_match_with_priors_falls_back_for_single_pet(data_dir, monkeypatch):
    """If only one pet exists, match_with_priors must short-circuit to
    the vanilla matcher (no priors to apply, would only add noise)."""
    from app import embeddings, recognition

    ref = [1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 1)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _fake_extractor(ref))
    pets = [_ref_pet("Mochi", ref)]
    out = recognition.match_with_priors(b"x", pets, camera="front")
    assert out.pet_id == "mochi"
    # Single-pet path doesn't populate the diagnostic fields.
    assert out.cosine_only is None


def test_extract_bbox_handles_frigate_0_13_shape(data_dir):
    from app import recognition
    event = {"id": "x", "data": {"box": [10, 20, 30, 40]}}
    assert recognition.extract_bbox_from_event(event) == (10.0, 20.0, 30.0, 40.0)


def test_extract_bbox_falls_back_to_top_level_box(data_dir):
    from app import recognition
    event = {"id": "x", "box": [1, 2, 3, 4]}
    assert recognition.extract_bbox_from_event(event) == (1.0, 2.0, 3.0, 4.0)


def test_extract_bbox_returns_none_for_malformed_or_missing(data_dir):
    from app import recognition
    assert recognition.extract_bbox_from_event({}) is None
    assert recognition.extract_bbox_from_event({"data": {"box": [1, 2]}}) is None
    assert recognition.extract_bbox_from_event({"box": "not a list"}) is None
    assert recognition.extract_bbox_from_event({"box": [1, 2, "x", 4]}) is None


def test_extract_bbox_falls_back_to_region_when_box_absent(data_dir):
    """In-progress events sometimes lack `data.box` but have `data.region`
    (the broader bounding box used for inference). Use it as a last-resort
    so we still get *some* bbox-aware prior signal."""
    from app import recognition
    event = {"id": "x", "data": {"region": [50, 60, 120, 140]}}
    assert recognition.extract_bbox_from_event(event) == (50.0, 60.0, 120.0, 140.0)
    # And `box` still wins when both are present.
    event2 = {"id": "y", "data": {"box": [1, 2, 3, 4],
                                    "region": [50, 60, 120, 140]}}
    assert recognition.extract_bbox_from_event(event2) == (1.0, 2.0, 3.0, 4.0)


def test_match_with_priors_records_diagnostics(data_dir, monkeypatch):
    """Multi-pet path populates cosine_only and prior_boost so the UI
    can show "matched on cosine + priors" for transparency."""
    from app import embeddings, recognition

    a_ref = [1.0, 0.0] + [0.0] * (embeddings.EMBEDDING_DIM - 2)
    b_ref = [0.0, 1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 2)
    query = [0.0, 0.95, 0.31] + [0.0] * (embeddings.EMBEDDING_DIM - 3)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _fake_extractor(query))
    pets = [_ref_pet("Mochi", a_ref), _ref_pet("Maru", b_ref)]
    _seed_history([])  # no history → priors are 0

    out = recognition.match_with_priors(b"x", pets, camera="front")
    assert out.pet_id == "maru"
    assert out.cosine_only is not None
    assert out.prior_boost is not None
    # No history → boost should be ≈ 0.
    assert abs(out.prior_boost) < 1e-6
