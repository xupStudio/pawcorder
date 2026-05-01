"""Pro: cloud-trained per-tenant pet recognition model.

The flow, end-to-end:

  1. Owner opens ``/pets/{pet}/train-cloud``, ticks the consent box,
     drops 20–60 reference photos onto the page.
  2. Admin streams them to the Pawcorder relay over mTLS-style auth
     (license-key bearer). Photos are encrypted at rest on the relay
     and bound to the (license, pet) tuple — never visible to humans.
  3. Relay queues a training job. The kernel is a logistic-regression
     head on top of the same MobileNetV3-Small embeddings the local
     recognition uses (see embeddings.py). Negative samples come from
     a public dataset (Oxford-IIIT Pet) so we never co-mingle
     across tenants.
  4. Relay produces a small (KB-scale) sklearn model + a SHA-256
     deletion receipt for the photos, then deletes the photos.
  5. Admin pulls the model + receipt over the same channel, stores
     the model at ``/data/models/petclf-<pet>.joblib``, recognition
     loads it on next boot.

This module owns the **client side** of all that: state tracking,
upload streaming, model fetch, receipt verification. The actual
kernel + storage live on the relay (see ``relay.cloud_train``).

Job state on disk
-----------------

We persist one NDJSON line per state transition to
``/data/config/cloud_train.ndjson``. Read it newest-first to recover
"what's going on" after a restart. Schema:

    {"ts": 1714440000, "pet_id": "mochi",
     "status": "uploading"|"queued"|"training"|"ready"|"failed",
     "uploaded_count": 0, "total_count": 0,
     "receipt": "<sha256>"|null,
     "error": "...optional..."}

The page polls ``/api/pets/{pet}/train-cloud/status`` for live updates.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import config_store

logger = logging.getLogger("pawcorder.cloud_train")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
STATE_PATH = DATA_DIR / "config" / "cloud_train.ndjson"
MODELS_DIR = DATA_DIR / "models"

RELAY_BASE = os.environ.get(
    "PAWCORDER_RELAY_BASE", "https://relay.pawcorder.app",
)
UPLOAD_TIMEOUT_SECONDS = 60.0
POLL_TIMEOUT_SECONDS = 15.0
MODEL_DOWNLOAD_TIMEOUT_SECONDS = 30.0

# Reasonable per-photo size + count caps. A vet-quality reference
# photoset is ~30 images @ ~500 KB each ≈ 15 MB. Anything bigger is
# usually the owner forgetting to crop, and we'd rather reject in the
# UI than discover at the relay end.
MAX_PHOTO_BYTES = 8 * 1024 * 1024     # 8 MB per file
MAX_TOTAL_PHOTOS = 80
ALLOWED_MIME = ("image/jpeg", "image/png", "image/webp")

# Same shape PetStore.slugify produces — kept here so the local model
# path build (``MODELS_DIR / f"petclf-{pet_id}.joblib"``) can never
# escape the data dir, even if a future migration loosens slug rules.
_PET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


def _validate_pet_id(pet_id: str) -> None:
    if not isinstance(pet_id, str) or not _PET_ID_RE.match(pet_id):
        raise CloudTrainError("cloud_train_invalid_pet_id")


@dataclass
class JobState:
    """Latest known state for one pet's training job."""
    pet_id: str
    status: str = "idle"        # idle | uploading | queued | training | ready | failed
    uploaded_count: int = 0
    total_count: int = 0
    receipt: Optional[str] = None
    error: Optional[str] = None
    updated_at: float = 0.0
    model_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "pet_id": self.pet_id,
            "status": self.status,
            "uploaded_count": self.uploaded_count,
            "total_count": self.total_count,
            "receipt": self.receipt,
            "error": self.error,
            "updated_at": self.updated_at,
            "model_path": self.model_path,
        }


# ---- on-disk state ledger ---------------------------------------------

_state_lock = threading.Lock()


def _append_state(state: JobState) -> None:
    """Append-only state ledger. We never mutate prior rows so a
    restart can reconstruct the timeline of a job. The lock makes
    interleaved writes from concurrent trainings (one per pet)
    line-atomic — without it, two simultaneous appends could
    fragment a JSON line and break ``latest_state``'s parser."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.time()
    line = json.dumps({"ts": state.updated_at, **state.to_dict()},
                       ensure_ascii=False)
    with _state_lock, STATE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def latest_state(pet_id: str) -> JobState:
    """Newest-first scan of the ledger. Returns an idle state when
    the pet has no history yet."""
    if not STATE_PATH.exists():
        return JobState(pet_id=pet_id)
    try:
        # Read the whole file — it's bounded (one job per few weeks per
        # pet, and we cap at ~50 rows per job). Streaming would be
        # over-engineering for this volume.
        rows = STATE_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return JobState(pet_id=pet_id)
    for line in reversed(rows):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("pet_id") == pet_id:
            return JobState(
                pet_id=row.get("pet_id", pet_id),
                status=row.get("status", "idle"),
                uploaded_count=int(row.get("uploaded_count") or 0),
                total_count=int(row.get("total_count") or 0),
                receipt=row.get("receipt"),
                error=row.get("error"),
                updated_at=float(row.get("ts") or 0),
                model_path=row.get("model_path"),
            )
    return JobState(pet_id=pet_id)


def all_pets_with_jobs() -> list[str]:
    """Distinct pet_ids that have ever had a job. Used by the dashboard
    badge that lists in-flight trainings."""
    if not STATE_PATH.exists():
        return []
    seen: set[str] = set()
    try:
        for line in STATE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = row.get("pet_id")
            if pid:
                seen.add(pid)
    except OSError:
        return []
    return sorted(seen)


# ---- relay calls -------------------------------------------------------

class CloudTrainError(RuntimeError):
    """Surfaced to the UI as a toast — keep messages user-readable."""


def _license_or_raise() -> str:
    cfg = config_store.load_config()
    if not cfg.pawcorder_pro_license_key:
        raise CloudTrainError("cloud_train_requires_pro_license")
    return cfg.pawcorder_pro_license_key


async def upload_photos(pet_id: str, photos: list[tuple[str, bytes, str]],
                          *, consent_text_hash: str) -> JobState:
    """Stream every photo to the relay in one multipart POST.

    ``photos`` is ``[(filename, bytes, mime), ...]`` — already validated
    by the caller (size + mime type). We take an explicit
    ``consent_text_hash`` so the relay can store proof the owner saw
    the version of the consent text we showed them — auditable later
    without us having to keep a copy of every consent string ever
    rendered.

    Returns the new JobState. Raises ``CloudTrainError`` on relay /
    license / network failure with a translated key the UI surfaces.
    """
    _validate_pet_id(pet_id)
    license_key = _license_or_raise()
    if not photos:
        raise CloudTrainError("cloud_train_no_photos")
    if len(photos) > MAX_TOTAL_PHOTOS:
        raise CloudTrainError("cloud_train_too_many_photos")

    # Mark "uploading" first so a cancelled / crashed run is visible
    # in the ledger (and the UI doesn't show stale "ready" state).
    state = JobState(
        pet_id=pet_id, status="uploading",
        uploaded_count=0, total_count=len(photos),
    )
    _append_state(state)

    files = [
        ("photos", (filename, body, mime))
        for (filename, body, mime) in photos
    ]
    data = {
        "license_key": license_key,
        "pet_id": pet_id,
        "consent_hash": consent_text_hash,
    }
    try:
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{RELAY_BASE}/v1/cloud-train/upload",
                data=data, files=files,
            )
    except httpx.HTTPError as exc:
        state.status = "failed"
        state.error = f"network: {exc}"[:200]
        _append_state(state)
        raise CloudTrainError("cloud_train_relay_unreachable") from exc

    if resp.status_code == 401:
        state.status = "failed"
        state.error = "license_invalid"
        _append_state(state)
        raise CloudTrainError("cloud_train_license_invalid")
    if resp.status_code == 413:
        state.status = "failed"
        state.error = "photos_too_large"
        _append_state(state)
        raise CloudTrainError("cloud_train_photos_too_large")
    if resp.status_code != 200:
        state.status = "failed"
        state.error = f"relay_{resp.status_code}"
        _append_state(state)
        raise CloudTrainError("cloud_train_relay_error")

    body = resp.json() if resp.content else {}
    state.status = body.get("status", "queued")
    state.uploaded_count = int(body.get("uploaded_count") or len(photos))
    _append_state(state)
    return state


async def poll_status(pet_id: str) -> JobState:
    """Ask the relay for the latest status, write it to the ledger, and
    return it. UI calls this on a 5-10s timer while a job is in
    flight."""
    _validate_pet_id(pet_id)
    license_key = _license_or_raise()
    try:
        async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{RELAY_BASE}/v1/cloud-train/status",
                params={"license_key": license_key, "pet_id": pet_id},
            )
    except httpx.HTTPError as exc:
        # A network blip shouldn't churn the ledger — return cached
        # state instead and hint the user network is unreliable.
        cached = latest_state(pet_id)
        cached.error = f"poll_network: {exc}"[:200]
        return cached

    if resp.status_code != 200:
        cached = latest_state(pet_id)
        if resp.status_code == 401:
            cached.error = "license_invalid"
        return cached

    body = resp.json() if resp.content else {}
    state = JobState(
        pet_id=pet_id,
        status=str(body.get("status") or "idle"),
        uploaded_count=int(body.get("uploaded_count") or 0),
        total_count=int(body.get("total_count") or 0),
        receipt=body.get("receipt"),
        error=body.get("error"),
    )
    # If the model is ready and we haven't pulled it yet, do that now
    # in the same call — saves the UI an extra round trip.
    if state.status == "ready" and not _model_already_local(pet_id):
        try:
            state.model_path = await _download_model(pet_id, license_key)
        except CloudTrainError as exc:
            # Don't leave the user staring at "ready" while the model
            # silently failed to land — flip to failed so the UI can
            # offer a retry instead of a misleading green tick.
            state.status = "failed"
            state.error = str(exc)
    elif state.status == "ready":
        state.model_path = str(_local_model_path(pet_id))
    _append_state(state)
    return state


def _local_model_path(pet_id: str) -> Path:
    return MODELS_DIR / f"petclf-{pet_id}.joblib"


def _model_already_local(pet_id: str) -> bool:
    return _local_model_path(pet_id).exists()


async def _download_model(pet_id: str, license_key: str) -> str:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    target = _local_model_path(pet_id)
    # Hard cap on the body size: a real classifier head is KB-scale.
    # 10 MB leaves slack for future swap-ins; a compromised relay can't
    # fill /data unbounded.
    max_bytes = 10 * 1024 * 1024
    try:
        async with httpx.AsyncClient(timeout=MODEL_DOWNLOAD_TIMEOUT_SECONDS) as client:
            async with client.stream(
                "GET",
                f"{RELAY_BASE}/v1/cloud-train/model",
                params={"license_key": license_key, "pet_id": pet_id},
            ) as resp:
                if resp.status_code != 200:
                    raise CloudTrainError("cloud_train_model_download_failed")
                total = 0
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise CloudTrainError("cloud_train_model_too_large")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise CloudTrainError("cloud_train_model_download_failed") from exc
    # Atomic write — recognition._load_cloud_model is allowed to read
    # this file at any time (the matcher path runs on every event), so
    # a torn write would let it see a half-flushed pickle. Stage to
    # ``.downloading`` then ``os.replace`` to swap atomically.
    tmp = target.with_suffix(target.suffix + ".downloading")
    tmp.write_bytes(b"".join(chunks))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    import os as _os
    _os.replace(tmp, target)
    return str(target)


async def request_delete(pet_id: str) -> JobState:
    """Tell the relay to purge any uploaded photos for this pet — and
    delete the local model. Returns the new JobState (idle).

    Used by the "forget my training data" button on the train page.
    Always best-effort: even if the relay errors, we reset local state
    so the user isn't stuck."""
    _validate_pet_id(pet_id)
    license_key = _license_or_raise()
    try:
        async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS) as client:
            await client.post(
                f"{RELAY_BASE}/v1/cloud-train/delete",
                json={"license_key": license_key, "pet_id": pet_id},
            )
    except httpx.HTTPError:
        pass    # ignore — we still purge locally
    target = _local_model_path(pet_id)
    if target.exists():
        try:
            target.unlink()
        except OSError as exc:
            logger.warning("local model unlink failed: %s", exc)
    state = JobState(pet_id=pet_id, status="idle")
    _append_state(state)
    return state


# ---- consent text + hashing -------------------------------------------

def consent_hash(text: str) -> str:
    """SHA-256 of the consent string the user just clicked through.

    The relay records this hash alongside the upload, so we can later
    prove which version of the consent the owner agreed to without
    keeping a copy of every consent string we've ever rendered."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- file validation ---------------------------------------------------

def validate_file(filename: str, body: bytes, mime: str) -> Optional[str]:
    """Reject obvious junk before we burn a relay round trip on it.

    Returns ``None`` when the file is fine, an i18n key when it's not.
    The UI maps the key to a translated message — we never invent
    free-text strings here that bypass i18n.
    """
    if mime not in ALLOWED_MIME:
        return "cloud_train_bad_filetype"
    if len(body) > MAX_PHOTO_BYTES:
        return "cloud_train_file_too_big"
    # Cheap magic-byte sniff so a renamed text file gets caught even if
    # the browser sets the wrong MIME. We don't need a full image lib —
    # just enough to know "this looks like an image of the claimed type".
    head = body[:12]
    if mime == "image/jpeg" and not head.startswith(b"\xff\xd8\xff"):
        return "cloud_train_bad_jpeg"
    if mime == "image/png" and not head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "cloud_train_bad_png"
    if mime == "image/webp" and (b"WEBP" not in body[:32]):
        return "cloud_train_bad_webp"
    if not filename or len(filename) > 200:
        return "cloud_train_bad_filename"
    return None
