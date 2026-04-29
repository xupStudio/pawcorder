"""Tests for pets_store — CRUD + photo persistence + atomic save."""
from __future__ import annotations

import pytest


def test_load_empty(data_dir):
    from app.pets_store import PetStore
    assert PetStore().load() == []


def test_create_pet_generates_slug(data_dir):
    from app.pets_store import PetStore
    store = PetStore()
    pet = store.create(name="Mochi", species="cat")
    assert pet.pet_id == "mochi"
    assert pet.name == "Mochi"
    assert pet.species == "cat"


def test_create_pet_cjk_name_gets_hash_slug(data_dir):
    from app.pets_store import PetStore
    store = PetStore()
    pet = store.create(name="麻糬", species="cat")
    # Pure CJK → slug is pet_<hex>, deterministic for the same name.
    assert pet.pet_id.startswith("pet_")
    assert len(pet.pet_id) <= 32


def test_create_pet_mixed_name_keeps_ascii(data_dir):
    from app.pets_store import PetStore
    pet = PetStore().create(name="Mochi 麻糬", species="cat")
    assert pet.pet_id == "mochi"  # CJK part dropped from slug, ASCII kept


def test_create_pet_duplicate_name_rejected(data_dir):
    from app.pets_store import PetStore, PetValidationError
    store = PetStore()
    store.create(name="Mochi", species="cat")
    with pytest.raises(PetValidationError):
        store.create(name="Mochi", species="cat")


def test_create_pet_invalid_species_rejected(data_dir):
    from app.pets_store import PetStore, PetValidationError
    with pytest.raises(PetValidationError):
        PetStore().create(name="Mochi", species="parrot")


@pytest.mark.parametrize("bad_name", ["", "x" * 50, "<script>"])
def test_create_pet_invalid_name_rejected(data_dir, bad_name):
    from app.pets_store import PetStore, PetValidationError
    with pytest.raises(PetValidationError):
        PetStore().create(name=bad_name, species="cat")


def test_create_pet_two_cjk_pets_get_unique_slugs(data_dir):
    """Two pets with pure-CJK names produce stable but different slugs
    so their photo dirs don't collide on disk."""
    from app.pets_store import PetStore
    store = PetStore()
    a = store.create(name="麻糬", species="cat")
    b = store.create(name="麻吉", species="cat")
    assert a.pet_id != b.pet_id
    # Both follow the pet_<hex> pattern.
    assert a.pet_id.startswith("pet_") and b.pet_id.startswith("pet_")


def test_update_pet_renames_keeps_id(data_dir):
    from app.pets_store import PetStore
    store = PetStore()
    pet = store.create(name="Mochi", species="cat")
    updated = store.update(pet.pet_id, name="Mochi 大王")
    assert updated.pet_id == "mochi"  # immutable
    assert updated.name == "Mochi 大王"


def test_update_unknown_pet_raises(data_dir):
    from app.pets_store import PetStore
    with pytest.raises(KeyError):
        PetStore().update("ghost", name="x")


def test_delete_removes_yaml_entry_and_photo_dir(data_dir):
    from app.pets_store import PetStore
    store = PetStore()
    pet = store.create(name="Mochi", species="cat")
    store.add_photo(pet.pet_id, b"\xff\xd8\xff\xe0", [0.1] * 576)
    photo_dir = store.photo_dir / pet.pet_id
    assert photo_dir.exists()
    assert store.delete(pet.pet_id) is True
    assert store.get(pet.pet_id) is None
    assert not photo_dir.exists()


def test_delete_unknown_pet_returns_false(data_dir):
    from app.pets_store import PetStore
    assert PetStore().delete("ghost") is False


def test_add_photo_persists_bytes_and_embedding(data_dir):
    from app.pets_store import PetStore
    store = PetStore()
    pet = store.create(name="Mochi", species="cat")
    photo = store.add_photo(pet.pet_id, b"FAKE_JPEG", [0.5] * 576, ext=".jpg")
    assert photo.filename == "photo_01.jpg"
    on_disk = store.photo_path(pet.pet_id, photo.filename)
    assert on_disk.exists()
    assert on_disk.read_bytes() == b"FAKE_JPEG"
    # Embedding round-trips through YAML.
    reloaded = store.get(pet.pet_id)
    assert reloaded.photos[0].embedding == [0.5] * 576


def test_add_photo_capped_at_max(data_dir):
    """30 photo limit is enforced — guards against unbounded disk growth."""
    from app import pets_store
    from app.pets_store import PetStore, PetValidationError
    store = PetStore()
    pet = store.create(name="Mochi", species="cat")
    for i in range(pets_store.MAX_PHOTOS_PER_PET):
        store.add_photo(pet.pet_id, b"x", [0.0] * 576)
    with pytest.raises(PetValidationError):
        store.add_photo(pet.pet_id, b"x", [0.0] * 576)


def test_remove_photo_removes_disk_and_yaml(data_dir):
    from app.pets_store import PetStore
    store = PetStore()
    pet = store.create(name="Mochi", species="cat")
    photo = store.add_photo(pet.pet_id, b"FAKE", [0.2] * 576)
    on_disk = store.photo_path(pet.pet_id, photo.filename)
    assert on_disk.exists()
    assert store.remove_photo(pet.pet_id, photo.filename) is True
    assert not on_disk.exists()
    assert store.get(pet.pet_id).photos == []


def test_save_all_is_atomic(data_dir, monkeypatch):
    """Crash mid-rename must leave the previous pets.yml intact."""
    from app import pets_store, utils
    from app.pets_store import PetStore

    store = PetStore()
    store.create(name="Mochi", species="cat")

    def _boom(*_a, **_kw):
        raise OSError("simulated kill")
    monkeypatch.setattr(utils.os, "replace", _boom)

    with pytest.raises(OSError):
        store.create(name="Maru", species="cat")

    survivors = store.load()
    assert [p.name for p in survivors] == ["Mochi"]


def test_slugify_normalizes_special_chars(data_dir):
    from app.pets_store import slugify
    assert slugify("Mochi") == "mochi"
    assert slugify("MOCHI") == "mochi"
    # Hyphens, spaces, punctuation collapse to underscore — keeps the
    # slug safe for use as a directory name across all OSes.
    assert slugify("Mochi-Maru") == "mochi_maru"
    assert slugify(" Mochi ") == "mochi"
    assert slugify("Mochi 大王") == "mochi"
