"""Re-enroll loop tests.

We mock the embedding extractor so we can drive the loop without a
real ONNX session, and we use ``data_dir`` to point PetStore + photo
files at a temp tree per test.
"""
from __future__ import annotations

import numpy as np


class _StubExtractor:
    """Returns a deterministic vector keyed off the bytes — mirrors
    the production extractor's contract just enough for re-enroll's
    write path."""

    def __init__(self):
        self.calls: list[bytes] = []

    def extract(self, image_bytes: bytes):
        from app import embeddings
        self.calls.append(image_bytes)
        # Use a stable "all-ones" vector scaled by byte-length so the
        # re-enroll output is predictable across calls.
        v = np.full(embeddings.EMBEDDING_DIM, 0.1, dtype=np.float32)
        v[0] = float(len(image_bytes) % 100) / 100.0
        return embeddings.EmbeddingResult(vector=v / np.linalg.norm(v),
                                           success=True)


def _seed_pet_with_photos(data_dir, *, pet_id: str = "mochi",
                           backbone: str = "old_backbone",
                           photo_count: int = 3) -> None:
    """Write pet entry + on-disk photo files. The stale ``backbone``
    string makes re-enroll see them as needing work."""
    from app import pets_store

    photo_dir = pets_store.PETS_PHOTO_DIR / pet_id / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    photos = []
    for i in range(photo_count):
        fname = f"photo_{i + 1:02d}.jpg"
        # Real-ish bytes — tiny but enough that the stub extractor
        # returns a non-zero vector.
        (photo_dir / fname).write_bytes(b"\xff\xd8\xff" + bytes([i]) * 256)
        photos.append(pets_store.PetPhoto(
            filename=fname,
            embedding=[0.0] * 99,           # wrong dim → counts as stale
            backbone=backbone,
        ))
    pet = pets_store.Pet(
        pet_id=pet_id, name=pet_id.title(), species="cat",
        notes="", match_threshold=0.0, photos=photos,
    )
    pets_store.PetStore().save_all([pet])


def test_reenroll_updates_stale_photos(data_dir):
    from app import embeddings, pets_store, reenroll

    _seed_pet_with_photos(data_dir)
    embeddings.set_extractor(_StubExtractor())
    try:
        result = reenroll.reenroll_all()
        assert result.photos_total == 3
        assert result.photos_updated == 3
        assert result.photos_failed == 0
        assert result.photos_already_current == 0
        assert result.pets_updated == 1

        # The persisted store now has matching backbone + dim.
        pet = pets_store.PetStore().get("mochi")
        assert pet is not None
        for p in pet.photos:
            assert p.backbone == embeddings.active_backbone_name()
            assert len(p.embedding) == embeddings.EMBEDDING_DIM
    finally:
        embeddings.set_extractor(None)


def test_reenroll_idempotent_on_current_photos(data_dir):
    """Photos already at the active backbone get skipped — no re-embed."""
    from app import embeddings, pets_store, reenroll

    _seed_pet_with_photos(data_dir, backbone=embeddings.active_backbone_name())
    # The stale embeddings still have the wrong DIM, so they'll re-embed.
    # Update embeddings to the right dim so the only "stale" attribute
    # is gone — should be skipped entirely.
    pet = pets_store.PetStore().get("mochi")
    assert pet is not None
    for p in pet.photos:
        p.embedding = [0.0] * embeddings.EMBEDDING_DIM
    pets_store.PetStore().save_all([pet])

    stub = _StubExtractor()
    embeddings.set_extractor(stub)
    try:
        result = reenroll.reenroll_all()
        assert result.photos_already_current == 3
        assert result.photos_updated == 0
        assert stub.calls == []   # extractor never invoked
    finally:
        embeddings.set_extractor(None)


def test_reenroll_handles_missing_photo_file(data_dir):
    """A row in pets.yml without a backing file logs and counts as
    failed — does not nuke the embedding."""
    from app import embeddings, pets_store, reenroll

    _seed_pet_with_photos(data_dir)
    # Delete one underlying file before re-enroll.
    pet = pets_store.PetStore().get("mochi")
    assert pet is not None
    bad = pets_store.PETS_PHOTO_DIR / "mochi" / "photos" / pet.photos[0].filename
    bad.unlink()

    embeddings.set_extractor(_StubExtractor())
    try:
        result = reenroll.reenroll_all()
        assert result.photos_failed == 1
        assert result.photos_updated == 2     # other two succeeded
    finally:
        embeddings.set_extractor(None)


def test_stale_count_matches_visible_state(data_dir):
    from app import embeddings, pets_store, reenroll

    _seed_pet_with_photos(data_dir)
    assert reenroll.stale_count() == 3
    # Update one photo to be current; others stay stale.
    pet = pets_store.PetStore().get("mochi")
    pet.photos[0].embedding = [0.1] * embeddings.EMBEDDING_DIM
    pet.photos[0].backbone = embeddings.active_backbone_name()
    pets_store.PetStore().save_all([pet])
    assert reenroll.stale_count() == 2
