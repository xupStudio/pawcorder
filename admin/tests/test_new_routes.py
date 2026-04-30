"""Route-level tests for the new endpoints added in this drop:
tutorial page, onboarding reset, diary list/generate, Pro backfill,
and the extended /api/pets/health payload shape."""
from __future__ import annotations

import importlib
import pytest


def _has_pro() -> bool:
    """True when the Pro modules are installed (Pro repo). The OSS
    build's `app/pro/` directory is empty / absent so these imports
    raise ModuleNotFoundError → skip Pro-specific assertions."""
    try:
        importlib.import_module("app.pro.recognition_backfill_pro")
        return True
    except ModuleNotFoundError:
        return False


pro_only = pytest.mark.skipif(not _has_pro(), reason="Pro modules not installed")


# ---- /tutorial --------------------------------------------------------

def test_tutorial_page_renders(authed_client):
    resp = authed_client.get("/tutorial")
    assert resp.status_code == 200
    assert b"setup" in resp.content.lower() or b"\xe6\x95\x99\xe5\xad\xb8" in resp.content


def test_tutorial_page_redirects_when_unauthenticated(app_client):
    resp = app_client.get("/tutorial", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers.get("location", "")


# ---- /api/onboarding/reset --------------------------------------------

def test_onboarding_reset_clears_skip_set(authed_client):
    # Skip everything → confirm it's all-done from skip side.
    authed_client.post("/api/onboarding/skip", json={"all": True})
    state = authed_client.get("/api/onboarding").json()
    assert state["all_done"] is True

    # Reset → all steps pending again.
    resp = authed_client.post("/api/onboarding/reset", json={})
    assert resp.status_code == 200

    state = authed_client.get("/api/onboarding").json()
    assert state["all_done"] is False
    assert state["skipped_count"] == 0


# ---- /api/pets/diary --------------------------------------------------

def test_diary_list_reports_unconfigured_when_no_token(authed_client):
    resp = authed_client.get("/api/pets/diary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["backend"] is None
    assert body["diaries"] == []


def test_diary_list_filters_by_pet(authed_client):
    from app import pet_diary
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="mochi", pet_name="Mochi", date="2026-04-29",
        text="m", backend="openai", generated_at=1.0,
    ))
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="maru", pet_name="Maru", date="2026-04-29",
        text="r", backend="openai", generated_at=2.0,
    ))
    resp = authed_client.get("/api/pets/diary?pet_id=mochi")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["diaries"]) == 1
    assert body["diaries"][0]["pet_name"] == "Mochi"


def test_diary_generate_404_for_unknown_pet(authed_client):
    resp = authed_client.post("/api/pets/diary/generate", data={"pet_id": "ghost"})
    assert resp.status_code == 404


def test_diary_generate_400_when_unconfigured(authed_client):
    """No OpenAI key, no Pro license → 400 with diary_not_configured."""
    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})
    resp = authed_client.post("/api/pets/diary/generate", data={"pet_id": "mochi"})
    assert resp.status_code == 400
    assert "diary_not_configured" in resp.text


def test_diary_generate_swallows_backend_error_text(authed_client, monkeypatch):
    """If OpenAI returns garbage we must NOT echo the upstream body
    back to the client — it can carry API-key fragments."""
    from app import pet_diary
    authed_client.post("/api/pets", json={"name": "Mochi", "species": "cat"})

    async def boom(*a, **kw):
        raise RuntimeError("openai http 500: sk-leak-Bearer-xxxxx")

    # Preserve admin_session_secret etc. so authed_client's cookie
    # stays valid — only flip on the openai key for this test.
    from app import config_store
    real = config_store.load_config()
    real.openai_api_key = "sk-test"
    monkeypatch.setattr(config_store, "load_config", lambda: real)
    monkeypatch.setattr(pet_diary, "_call_openai", boom)

    resp = authed_client.post("/api/pets/diary/generate", data={"pet_id": "mochi"})
    assert resp.status_code == 502
    assert "sk-leak" not in resp.text
    assert "diary_backend_error" in resp.text


# ---- /api/pets/backfill/pro -------------------------------------------

@pro_only
def test_pro_backfill_progress_returns_available_flag(authed_client):
    resp = authed_client.get("/api/pets/backfill/pro/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True


def test_pro_backfill_progress_oss_returns_unavailable(authed_client):
    """OSS build: same route exists but reports `available: false` so
    the UI can hide the Pro-tier button instead of 404'ing. The
    `licensed` field is always present so the upgrade-prompt gate
    sees a stable shape regardless of build."""
    if _has_pro():
        pytest.skip("only meaningful on OSS")
    resp = authed_client.get("/api/pets/backfill/pro/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["licensed"] is False  # demo conftest doesn't set a license


@pro_only
def test_pro_backfill_returns_409_when_oss_running(authed_client, monkeypatch):
    from app import recognition_backfill
    busy = recognition_backfill.BackfillProgress(running=True)
    monkeypatch.setattr(recognition_backfill, "current_progress", lambda: busy)
    resp = authed_client.post("/api/pets/backfill/pro", json={"hours": 24})
    assert resp.status_code == 409


# ---- /api/pets/health -------------------------------------------------

def test_health_route_always_returns_three_keys(authed_client):
    """Both OSS and Pro builds return the same three keys; Pro fills
    them, OSS leaves them as empty lists. Stable schema → the /pets
    page template doesn't need a feature-flag cascade."""
    resp = authed_client.get("/api/pets/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {"snapshots", "litter", "fight_clusters"}
    for key in ("snapshots", "litter", "fight_clusters"):
        assert isinstance(body[key], list)
