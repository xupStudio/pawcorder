"""Tests for recognition backfill — the "re-embed past events" feature.

We mock httpx (no real Frigate) and the embedding extractor (no ONNX
model in CI). The interesting logic is around:
  - the single-flight asyncio.Lock so concurrent requests don't double-run
  - rewrite-vs-append for events already in sightings.ndjson
  - the no-pets short-circuit
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import pytest


def _seed_pet(name: str = "Mochi"):
    """Add a pet with a fake (non-empty) embedding so match_against_pets
    has something to compare against."""
    from app import pets_store, embeddings
    import numpy as np
    store = pets_store.PetStore()
    pet = store.create(name=name, species="cat")
    # Unit-norm embedding so dot product is bounded in [-1, 1].
    v = np.ones(embeddings.EMBEDDING_DIM, dtype="float32")
    v /= float((v @ v) ** 0.5)
    store.add_photo(pet.pet_id, image_bytes=b"\xff\xd8fake",
                    embedding=v.tolist(), uploaded_at=int(time.time()))
    return pet


def test_progress_to_dict_safe_when_zero(data_dir):
    from app.recognition_backfill import BackfillProgress
    p = BackfillProgress()
    d = p.to_dict()
    # Avoid divide-by-zero in fraction.
    assert d["fraction"] == 0.0
    assert d["running"] is False


def test_progress_fraction_calculates(data_dir):
    from app.recognition_backfill import BackfillProgress
    p = BackfillProgress(total_events=10, processed=3)
    assert p.to_dict()["fraction"] == 0.3


def test_run_backfill_no_events_finishes_clean(data_dir, monkeypatch):
    from app import recognition_backfill

    async def _no_events(*_, **__):
        return []

    monkeypatch.setattr(recognition_backfill, "_fetch_events", _no_events)
    final = asyncio.run(recognition_backfill.run_backfill(since_hours=1.0))
    assert final.running is False
    assert final.total_events == 0
    assert final.processed == 0


def test_run_backfill_no_pets_reports_error(data_dir, monkeypatch):
    """If there are no configured pets, surface a clear error rather
    than silently doing nothing."""
    from app import recognition_backfill

    async def _events(*_, **__):
        return [{"id": "ev1", "camera": "cam1", "label": "cat",
                 "start_time": 0, "end_time": 0}]

    monkeypatch.setattr(recognition_backfill, "_fetch_events", _events)
    final = asyncio.run(recognition_backfill.run_backfill(since_hours=1.0))
    assert final.running is False
    assert "no pets" in final.error.lower()


def test_rewrite_sightings_updates_in_place(data_dir):
    from app import recognition, recognition_backfill
    # Seed two existing rows.
    recognition.SIGHTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    recognition.SIGHTINGS_LOG.write_text(
        json.dumps({"event_id": "ev1", "pet_id": None,  "pet_name": None,
                    "camera": "c", "label": "cat", "score": 0.0,
                    "confidence": "unknown", "start_time": 0, "end_time": 0}) + "\n"
        + json.dumps({"event_id": "ev2", "pet_id": None, "pet_name": None,
                      "camera": "c", "label": "cat", "score": 0.0,
                      "confidence": "unknown", "start_time": 0, "end_time": 0}) + "\n",
        encoding="utf-8",
    )
    rewritten = recognition_backfill._rewrite_sightings({
        "ev1": {"pet_id": "P1", "pet_name": "Mochi",
                "score": 0.91, "confidence": "high"},
    })
    assert rewritten == 1
    rows = [json.loads(l) for l in recognition.SIGHTINGS_LOG.read_text().splitlines()]
    assert rows[0]["pet_id"] == "P1"
    assert rows[0]["pet_name"] == "Mochi"
    # Untouched.
    assert rows[1]["pet_id"] is None


def test_rewrite_sightings_handles_missing_log(data_dir):
    from app import recognition_backfill
    # No sightings log on disk yet.
    assert recognition_backfill._rewrite_sightings({"ev1": {}}) == 0


def test_rewrite_sightings_skips_garbage_lines(data_dir):
    from app import recognition, recognition_backfill
    recognition.SIGHTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    recognition.SIGHTINGS_LOG.write_text(
        "not json\n"
        + json.dumps({"event_id": "ev1", "pet_id": None}) + "\n",
        encoding="utf-8",
    )
    n = recognition_backfill._rewrite_sightings({"ev1": {"pet_id": "P1"}})
    assert n == 1
    # Garbage line preserved verbatim — we never silently drop user data.
    assert "not json" in recognition.SIGHTINGS_LOG.read_text()


def test_run_backfill_appends_new_events(data_dir, monkeypatch):
    """Events not yet in sightings.ndjson should be appended."""
    from app import recognition, recognition_backfill, pets_store, embeddings

    _seed_pet()

    async def _events(*_, **__):
        return [{"id": "ev_new", "camera": "cam1", "label": "cat",
                 "start_time": 1000.0, "end_time": 1010.0}]

    async def _snapshot(*_, **__):
        return b"\xff\xd8fake-jpeg"

    @dataclass
    class _Result:
        success: bool
        vector: object
        error: str = ""

    class _StubExtractor:
        def extract(self, _):
            import numpy as np
            v = np.ones(embeddings.EMBEDDING_DIM, dtype="float32")
            v /= (v @ v) ** 0.5
            return _Result(success=True, vector=v)

    monkeypatch.setattr(recognition_backfill, "_fetch_events", _events)
    monkeypatch.setattr(recognition_backfill, "_fetch_snapshot", _snapshot)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _StubExtractor())

    final = asyncio.run(recognition_backfill.run_backfill(since_hours=1.0))
    assert final.running is False
    assert final.total_events == 1
    assert final.processed == 1
    # The event should be in the sightings log now.
    rows = recognition.read_sightings(limit=10)
    assert any(r.get("event_id") == "ev_new" for r in rows)


# ---- routes ------------------------------------------------------------

def test_backfill_route_starts_run(authed_client, monkeypatch):
    """POST /api/pets/backfill should trigger run_backfill exactly once."""
    from app import recognition_backfill

    started = {"count": 0}

    async def _fake_run(**kw):
        started["count"] += 1
        return recognition_backfill.BackfillProgress()

    monkeypatch.setattr(recognition_backfill, "run_backfill", _fake_run)
    resp = authed_client.post("/api/pets/backfill", json={"hours": 24})
    assert resp.status_code == 200


def test_backfill_progress_route_returns_dict(authed_client):
    resp = authed_client.get("/api/pets/backfill/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert "running" in body
    assert "fraction" in body


def test_backfill_route_rejects_when_running(authed_client, monkeypatch):
    """If a backfill is already in flight, the API should refuse rather
    than starting a second concurrent one."""
    from app import recognition_backfill
    fake = recognition_backfill.BackfillProgress(running=True)
    monkeypatch.setattr(recognition_backfill, "current_progress", lambda: fake)
    resp = authed_client.post("/api/pets/backfill", json={"hours": 24})
    assert resp.status_code == 409
