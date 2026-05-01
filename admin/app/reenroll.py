"""Re-embed every stored reference photo against the active backbone.

Why this exists
---------------

The system page lets the operator pick an embedding backbone
(``mobilenetv3_small_100`` for speed, ``dinov2_small`` for fine-grained
multi-pet households). When the choice changes, every existing
``PetPhoto.embedding`` is in the wrong feature space — recognition's
filter at :func:`recognition.match_against_pets` skips them, so the
admin keeps running but the owner's pets become *un-recognisable*
until photos are re-embedded.

Re-enroll walks the on-disk photo files (which we always keep around),
runs them through the currently-active extractor, and overwrites
``embedding`` + ``backbone`` for each. No new uploads required.

Usage flow
----------

  1. Operator changes ``PAWCORDER_EMBEDDING_BACKBONE`` on the System
     page (or in .env directly).
  2. Admin restart — the new backbone is now ``active_backbone_name``.
  3. Recognition silently mismatches because every stored photo says
     ``backbone="mobilenetv3_small_100"`` and active is now
     ``"dinov2_small"``. The System page surfaces a "needs re-enroll"
     warning chip with a count.
  4. Operator clicks "Re-enroll all photos" → this module's
     :func:`reenroll_all` runs synchronously per pet.
  5. ``PetPhoto.backbone`` rows update to the active name; recognition
     starts matching again.

The walk is idempotent — re-running on already-current embeddings is
a no-op (we skip rows whose backbone matches the active one). Soft-
fails per photo: a corrupt JPEG or a missing file is logged and the
row is left as-is rather than nuked, so a partial re-enroll never
loses an embedding it couldn't replace.

License posture: pure-stdlib + numpy. No new deps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import embeddings
from .pets_store import Pet, PetPhoto, PetStore

logger = logging.getLogger("pawcorder.reenroll")


@dataclass
class ReenrollResult:
    """One run's summary — the API surface that /api/pets/reenroll
    returns and the page renders."""
    backbone: str                 # which backbone we re-embedded against
    pets_total: int               # how many pets were considered
    pets_updated: int             # how many had at least one photo re-embedded
    photos_total: int             # photos seen across all pets
    photos_updated: int           # successfully re-embedded
    photos_failed: int            # decode / file-missing / inference error
    photos_already_current: int   # skipped because backbone already matched

    def to_dict(self) -> dict:
        return {
            "backbone": self.backbone,
            "pets_total": self.pets_total,
            "pets_updated": self.pets_updated,
            "photos_total": self.photos_total,
            "photos_updated": self.photos_updated,
            "photos_failed": self.photos_failed,
            "photos_already_current": self.photos_already_current,
        }


def _photo_path_for(store: PetStore, pet_id: str, photo: PetPhoto) -> Path:
    """Mirror PetStore's on-disk layout for a single reference photo.
    Kept private — the re-enroll loop is the only caller."""
    return store.photo_dir / pet_id / "photos" / photo.filename


def reenroll_all(store: PetStore | None = None) -> ReenrollResult:
    """Re-embed every photo whose backbone is stale relative to the
    currently active one. Returns a structured summary suitable for
    the JSON API + the page's toast.

    Synchronous on purpose — ~30 ms / photo on Pi-class CPU, so even
    10 pets × 30 photos ≈ 9 s, fast enough to block the HTTP handler
    under a reasonable timeout. If we ever raise the photo cap, push
    this onto the PollingTask thread pool.

    Holds ``PetStore._write_lock`` for the whole load-embed-save cycle
    so a concurrent ``add_photo`` from another worker can't interleave
    and lose its append when our slower ``save_all`` lands second.
    """
    store = store or PetStore()
    active = embeddings.active_backbone_name()
    extractor = embeddings.get_extractor()

    with PetStore._write_lock:
        pets = store.load()
        photos_total = 0
        photos_updated = 0
        photos_failed = 0
        photos_already_current = 0
        pets_updated = 0

        for pet in pets:
            any_changed = False
            for photo in pet.photos:
                photos_total += 1
                if photo.backbone == active and len(photo.embedding) == embeddings.EMBEDDING_DIM:
                    # Already in the right feature space — skip the
                    # re-embed cost. This makes re-enroll idempotent.
                    photos_already_current += 1
                    continue
                path = _photo_path_for(store, pet.pet_id, photo)
                if not path.exists():
                    logger.warning("reenroll: photo file missing for %s/%s",
                                    pet.pet_id, photo.filename)
                    photos_failed += 1
                    continue
                try:
                    image_bytes = path.read_bytes()
                except OSError as exc:
                    logger.warning("reenroll: read failed for %s: %s", path, exc)
                    photos_failed += 1
                    continue
                result = extractor.extract(image_bytes)
                if not result.success or result.vector.size == 0:
                    logger.warning("reenroll: embed failed for %s: %s",
                                    photo.filename, result.error)
                    photos_failed += 1
                    continue
                photo.embedding = [float(x) for x in result.vector]
                photo.backbone = active
                photos_updated += 1
                any_changed = True
            if any_changed:
                pets_updated += 1

        if pets_updated > 0:
            store.save_all(pets)

    return ReenrollResult(
        backbone=active,
        pets_total=len(pets),
        pets_updated=pets_updated,
        photos_total=photos_total,
        photos_updated=photos_updated,
        photos_failed=photos_failed,
        photos_already_current=photos_already_current,
    )


def stale_count(store: PetStore | None = None) -> int:
    """Count photos whose backbone doesn't match the active one.

    Cheap (no inference) — used by the /pets page to show a "Re-enroll
    needed" badge that stays accurate across backbone swaps.
    """
    store = store or PetStore()
    active = embeddings.active_backbone_name()
    return sum(
        1 for pet in store.load() for ph in pet.photos
        if ph.backbone != active or len(ph.embedding) != embeddings.EMBEDDING_DIM
    )
