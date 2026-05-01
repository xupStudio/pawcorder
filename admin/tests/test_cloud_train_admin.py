"""Tests for the admin-side cloud-train state + validation.

We exercise the file-validator, the local state ledger, and the
pet-id validator. The relay-side flow (multipart upload, encryption,
training stub) is tested separately under relay/tests.
"""
from __future__ import annotations


# ---- file validation ---------------------------------------------------

def test_validate_file_accepts_jpeg(data_dir):
    from app import cloud_train
    body = b"\xff\xd8\xff\xe0" + b"x" * 1000
    assert cloud_train.validate_file("a.jpg", body, "image/jpeg") is None


def test_validate_file_accepts_png(data_dir):
    from app import cloud_train
    body = b"\x89PNG\r\n\x1a\n" + b"x" * 1000
    assert cloud_train.validate_file("a.png", body, "image/png") is None


def test_validate_file_rejects_renamed_text(data_dir):
    """Browser sets image/jpeg but bytes are obviously not a JPEG."""
    from app import cloud_train
    err = cloud_train.validate_file("a.jpg", b"hello world", "image/jpeg")
    assert err == "cloud_train_bad_jpeg"


def test_validate_file_rejects_oversize(data_dir):
    from app import cloud_train
    body = b"\xff\xd8\xff\xe0" + b"x" * (cloud_train.MAX_PHOTO_BYTES + 1)
    err = cloud_train.validate_file("a.jpg", body, "image/jpeg")
    assert err == "cloud_train_file_too_big"


def test_validate_file_rejects_unknown_mime(data_dir):
    from app import cloud_train
    err = cloud_train.validate_file("a.heic", b"any", "image/heic")
    assert err == "cloud_train_bad_filetype"


# ---- pet_id validator --------------------------------------------------

def test_pet_id_validator_rejects_traversal(data_dir):
    from app import cloud_train
    import pytest
    for bad in ("../etc", "/etc/passwd", "..", "", "MoChi"):
        with pytest.raises(cloud_train.CloudTrainError):
            cloud_train._validate_pet_id(bad)


def test_pet_id_validator_accepts_slug(data_dir):
    from app import cloud_train
    cloud_train._validate_pet_id("mochi")
    cloud_train._validate_pet_id("mochi_2")
    cloud_train._validate_pet_id("black-cat")


# ---- state ledger ------------------------------------------------------

def test_latest_state_returns_idle_when_no_history(data_dir):
    from app import cloud_train
    s = cloud_train.latest_state("mochi")
    assert s.status == "idle"
    assert s.pet_id == "mochi"


def test_latest_state_returns_newest_row(data_dir):
    from app import cloud_train
    cloud_train._append_state(cloud_train.JobState(
        pet_id="mochi", status="uploading", uploaded_count=0, total_count=10,
    ))
    cloud_train._append_state(cloud_train.JobState(
        pet_id="mochi", status="ready", receipt="abc",
    ))
    # Other pet's transitions don't bleed into mochi's state.
    cloud_train._append_state(cloud_train.JobState(
        pet_id="maru", status="failed", error="boom",
    ))
    s = cloud_train.latest_state("mochi")
    assert s.status == "ready"
    assert s.receipt == "abc"


def test_consent_hash_is_stable_sha256(data_dir):
    from app import cloud_train
    h1 = cloud_train.consent_hash("hello")
    h2 = cloud_train.consent_hash("hello")
    h3 = cloud_train.consent_hash("hello!")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64    # SHA-256 hex


# ---- has_cloud_model (recognition hook) -------------------------------

def test_has_cloud_model_false_when_missing(data_dir):
    from app import recognition
    assert recognition.has_cloud_model("mochi") is False


def test_has_cloud_model_true_with_magic_header(data_dir):
    from app import recognition
    recognition.CLOUD_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    path = recognition.CLOUD_MODELS_DIR / "petclf-mochi.joblib"
    path.write_bytes(recognition.CLOUD_MODEL_MAGIC + b"...payload...")
    assert recognition.has_cloud_model("mochi") is True


def test_has_cloud_model_false_for_bad_magic(data_dir):
    from app import recognition
    recognition.CLOUD_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    path = recognition.CLOUD_MODELS_DIR / "petclf-mochi.joblib"
    path.write_bytes(b"NOTPWCDR" + b"\x00" * 32)
    assert recognition.has_cloud_model("mochi") is False


def test_has_cloud_model_rejects_traversal(data_dir):
    from app import recognition
    # Even if a file *did* exist at the traversed path, the validator
    # should refuse to read it.
    assert recognition.has_cloud_model("../etc") is False
