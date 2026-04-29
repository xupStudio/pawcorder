"""End-to-end route tests for /api/pets and the /pets page."""
from __future__ import annotations

from io import BytesIO

import numpy as np
import pytest


# ---- helpers -----------------------------------------------------------

def _stub_extractor(monkeypatch):
    """Force embeddings.get_extractor() to return a deterministic fake
    so route tests don't need the real ONNX model."""
    from app import embeddings
    from app.embeddings import EmbeddingResult

    class _Fake:
        def load(self): return True
        def extract(self, _b):
            v = np.ones(embeddings.EMBEDDING_DIM, dtype=np.float32)
            v = v / np.linalg.norm(v)
            return EmbeddingResult(vector=v, success=True)
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _Fake())


def _tiny_jpeg() -> bytes:
    """16-byte token that the extractor stub accepts as 'an image'.
    No actual decoding happens because the stub bypasses preprocessing."""
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"


# ---- page renders ------------------------------------------------------

def test_pets_page_renders(authed_client):
    resp = authed_client.get("/pets")
    assert resp.status_code == 200
    assert "pawcorder" in resp.text


def test_pets_page_redirects_when_unauthenticated(app_client):
    resp = app_client.get("/pets", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---- pet CRUD ----------------------------------------------------------

def test_create_pet_via_api(authed_client):
    resp = authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    assert resp.status_code == 200
    pet = resp.json()["pet"]
    assert pet["pet_id"] == "mochi"
    assert pet["name"] == "Mochi"


def test_create_pet_invalid_species_400(authed_client):
    resp = authed_client.post("/api/pets", json={"name": "Mochi", "species": "parrot"})
    assert resp.status_code == 400


def test_list_pets(authed_client):
    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    authed_client.post("/api/pets", json={"name": "Maru", "species": "cat"})
    resp = authed_client.get("/api/pets")
    assert resp.status_code == 200
    body = resp.json()
    assert {p["name"] for p in body["pets"]} == {"Mochi", "Maru"}
    assert "recognition_ready" in body


def test_update_pet(authed_client):
    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    resp = authed_client.put("/api/pets/mochi", json={"notes": "黑色短毛"})
    assert resp.status_code == 200
    assert resp.json()["pet"]["notes"] == "黑色短毛"


def test_update_unknown_pet_404(authed_client):
    resp = authed_client.put("/api/pets/ghost", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_pet(authed_client):
    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    resp = authed_client.delete("/api/pets/mochi")
    assert resp.status_code == 200
    assert authed_client.get("/api/pets").json()["pets"] == []


def test_delete_unknown_pet_404(authed_client):
    assert authed_client.delete("/api/pets/ghost").status_code == 404


# ---- photo upload ------------------------------------------------------

def test_add_photo_uses_extractor(authed_client, monkeypatch):
    _stub_extractor(monkeypatch)
    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})

    resp = authed_client.post(
        "/api/pets/mochi/photos",
        files={"file": ("mochi.jpg", _tiny_jpeg(), "image/jpeg")},
    )
    assert resp.status_code == 200, resp.text

    listing = authed_client.get("/api/pets").json()["pets"]
    assert listing[0]["photo_count"] == 1


def test_add_photo_unknown_pet_404(authed_client, monkeypatch):
    _stub_extractor(monkeypatch)
    resp = authed_client.post(
        "/api/pets/ghost/photos",
        files={"file": ("g.jpg", _tiny_jpeg(), "image/jpeg")},
    )
    assert resp.status_code == 404


def test_add_photo_empty_400(authed_client, monkeypatch):
    _stub_extractor(monkeypatch)
    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    resp = authed_client.post(
        "/api/pets/mochi/photos",
        files={"file": ("empty.jpg", b"", "image/jpeg")},
    )
    assert resp.status_code == 400


def test_add_photo_when_extractor_fails_503(authed_client, monkeypatch):
    """If the model isn't loaded, the route returns 503 with a helpful
    pointer to /api/pets/setup-model — not a 500 with a stack trace."""
    from app import embeddings
    from app.embeddings import EmbeddingResult
    import numpy as np

    class _Failing:
        def load(self): return False
        def extract(self, _b):
            return EmbeddingResult(
                vector=np.zeros(embeddings.EMBEDDING_DIM, dtype=np.float32),
                success=False, error="model not loaded",
            )
    monkeypatch.setattr(embeddings, "get_extractor", lambda: _Failing())

    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    resp = authed_client.post(
        "/api/pets/mochi/photos",
        files={"file": ("a.jpg", _tiny_jpeg(), "image/jpeg")},
    )
    assert resp.status_code == 503
    assert "model" in resp.json()["error"].lower()


def test_get_and_delete_photo(authed_client, monkeypatch):
    _stub_extractor(monkeypatch)
    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    authed_client.post(
        "/api/pets/mochi/photos",
        files={"file": ("a.jpg", _tiny_jpeg(), "image/jpeg")},
    )
    fname = authed_client.get("/api/pets").json()["pets"][0]["photos"][0]["filename"]
    # Fetch the photo bytes
    resp = authed_client.get(f"/api/pets/mochi/photos/{fname}")
    assert resp.status_code == 200
    assert resp.content == _tiny_jpeg()
    assert resp.headers["content-type"] == "image/jpeg"
    # Delete and confirm 404 on next fetch
    resp = authed_client.delete(f"/api/pets/mochi/photos/{fname}")
    assert resp.status_code == 200
    assert authed_client.get(f"/api/pets/mochi/photos/{fname}").status_code == 404


def test_photo_path_traversal_blocked_by_router(authed_client):
    """FastAPI's path param doesn't accept slashes by default, so a
    user can't send `..%2F..%2Fetc%2Fpasswd`. Belt-and-braces test."""
    resp = authed_client.get("/api/pets/mochi/photos/..%2Fpawcorder%2F.env")
    # FastAPI returns 404 — the route accepts only one path segment.
    assert resp.status_code == 404


# ---- timeline ----------------------------------------------------------

def test_timeline_for_unknown_pet_404(authed_client):
    resp = authed_client.get("/api/pets/ghost/timeline")
    assert resp.status_code == 404


def test_timeline_returns_journeys(authed_client, monkeypatch):
    """Seed a sighting then call /timeline."""
    import time
    from app import recognition

    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    now = time.time()
    recognition.append_sighting(recognition.Sighting(
        event_id="evt1", camera="kitchen", label="cat",
        pet_id="mochi", pet_name="Mochi",
        score=0.92, confidence="high",
        start_time=now - 60, end_time=now - 50,
    ))
    resp = authed_client.get("/api/pets/mochi/timeline")
    assert resp.status_code == 200
    journeys = resp.json()["journeys"]
    assert len(journeys) == 1
    assert journeys[0]["legs"][0]["camera"] == "kitchen"


# ---- model setup -------------------------------------------------------

def test_setup_model_route_invokes_download(authed_client, monkeypatch):
    from app import embeddings

    called = []
    def _stub(*, force=False, timeout=60.0):
        called.append(force)
        return True
    monkeypatch.setattr(embeddings, "download_model", _stub)

    resp = authed_client.post("/api/pets/setup-model", json={})
    assert resp.status_code == 200
    assert called == [False]  # called once with force=False


def test_setup_model_route_502_when_download_fails(authed_client, monkeypatch):
    from app import embeddings
    monkeypatch.setattr(embeddings, "download_model", lambda **_kw: False)
    resp = authed_client.post("/api/pets/setup-model", json={})
    assert resp.status_code == 502


# ---- Frigate snapshot proxy --------------------------------------------

def test_snapshot_route_accepts_real_frigate_id(authed_client, monkeypatch):
    """Real Frigate event ids look like '1730000000.123456-cam-cat'
    and contain dots + hyphens. The proxy MUST allow that."""
    import httpx
    from app import main as main_module

    class _FakeResp:
        status_code = 200
        content = b"\xff\xd8\xffJPEG"
        headers = {"content-type": "image/jpeg"}

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, _url, params=None): return _FakeResp()

    monkeypatch.setattr(main_module.httpx, "AsyncClient", lambda *a, **k: _FakeClient())
    resp = authed_client.get("/api/frigate/snapshot/1730000000.123456-kitchen-cat")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_snapshot_route_rejects_path_traversal(authed_client):
    """`..` and slashes are blocked — either by the router (404 because
    the URL doesn't match a single path segment) or by the in-handler
    whitelist (400). Both outcomes are safe."""
    for evil in ("..", "../etc", "aa/bb", "..%2Fetc"):
        resp = authed_client.get(f"/api/frigate/snapshot/{evil}")
        assert resp.status_code in (400, 404), f"got {resp.status_code} for {evil!r}"


def test_snapshot_route_rejects_garbage(authed_client):
    """Whitelist guards against control characters etc."""
    resp = authed_client.get("/api/frigate/snapshot/with%20space")
    # FastAPI URL-decodes "%20" → " ", which our regex rejects.
    assert resp.status_code == 400
