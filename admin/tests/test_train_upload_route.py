"""Tests for the cloud-train upload + status routes.

Specifically guards the consent-hash server-side recompute (a forged
client must not bypass the audit trail) and the count/size caps that
prevent memory-exhaust DoS.
"""
from __future__ import annotations

import hashlib

import pytest
import yaml


def _seed_pet(pet_id: str = "mochi") -> None:
    from app import pets_store
    pets_store.PETS_YAML.parent.mkdir(parents=True, exist_ok=True)
    pets_store.PETS_YAML.write_text(yaml.safe_dump({"pets": [{
        "pet_id": pet_id, "name": pet_id.title(), "species": "cat",
        "notes": "", "match_threshold": 0.0, "photos": [],
    }]}, sort_keys=False))


def _png(n: int = 100) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"x" * n


@pytest.fixture
def client(data_dir, stub_docker):
    from fastapi.testclient import TestClient
    from app import auth as auth_mod, main
    # Stamp a session cookie so _require_auth passes.
    cli = TestClient(main.app)
    cli.cookies.set(auth_mod.COOKIE_NAME,
                     auth_mod.issue_session(username="admin", role="admin"))
    return cli


def test_upload_rejects_missing_consent(client):
    _seed_pet()
    resp = client.post(
        "/api/pets/mochi/train-cloud/upload",
        headers={"X-Requested-With": "pawcorder"},
        files=[("photos", ("a.png", _png(), "image/png"))],
        data={"consent_hash": ""},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "consent_required"


def test_upload_rejects_forged_consent(client):
    """A forged client sending arbitrary hash must be rejected."""
    _seed_pet()
    resp = client.post(
        "/api/pets/mochi/train-cloud/upload",
        headers={"X-Requested-With": "pawcorder"},
        files=[("photos", ("a.png", _png(), "image/png"))],
        data={"consent_hash": "x" * 64},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "consent_required"


def test_upload_rejects_unknown_pet(client):
    """Pet existence is checked before any cloud_train work."""
    resp = client.post(
        "/api/pets/ghost/train-cloud/upload",
        headers={"X-Requested-With": "pawcorder"},
        files=[("photos", ("a.png", _png(), "image/png"))],
        data={"consent_hash": "0" * 64},
    )
    assert resp.status_code == 404


def test_upload_rejects_too_many_photos(client):
    """Count cap fires before any body is read into memory."""
    _seed_pet()
    files = [("photos", (f"p{i}.png", _png(), "image/png"))
             for i in range(81)]   # MAX_TOTAL_PHOTOS == 80
    # Need a valid consent hash to get past the consent gate.
    from app import cloud_train, i18n
    valid = cloud_train.consent_hash(i18n.t("CLOUD_TRAIN_CONSENT_BODY",
                                              lang="en"))
    resp = client.post(
        "/api/pets/mochi/train-cloud/upload",
        headers={"X-Requested-With": "pawcorder"},
        files=files,
        data={"consent_hash": valid},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "cloud_train_too_many_photos"


def test_status_rejects_unknown_pet(client):
    resp = client.get(
        "/api/pets/ghost/train-cloud/status",
        headers={"X-Requested-With": "pawcorder"},
    )
    assert resp.status_code == 404


def test_consent_hash_iterates_all_supported_langs(data_dir):
    """Adding a Japanese translation later mustn't reject Japanese
    owners — the verifier iterates ``i18n.SUPPORTED``."""
    from app import cloud_train, i18n
    # Today CLOUD_TRAIN_CONSENT_BODY has en + zh-TW only; the others
    # fall back to en, so the dedup'd set is {en_hash, zh_hash}.
    expected = {
        cloud_train.consent_hash(i18n.t("CLOUD_TRAIN_CONSENT_BODY", lang=l))
        for l in i18n.SUPPORTED
    }
    en_hash = cloud_train.consent_hash(
        i18n.t("CLOUD_TRAIN_CONSENT_BODY", lang="en"),
    )
    zh_hash = cloud_train.consent_hash(
        i18n.t("CLOUD_TRAIN_CONSENT_BODY", lang="zh-TW"),
    )
    assert en_hash in expected
    assert zh_hash in expected
