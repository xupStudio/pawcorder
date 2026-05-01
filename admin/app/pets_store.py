"""Per-pet storage: name, species, reference photos, embeddings.

Layout on disk:

    /data/config/pets.yml          metadata + embeddings (atomic write)
    /data/pets/<pet-id>/photos/    user-uploaded reference images

We keep embeddings in pets.yml (not as separate .npy files) so that:

  - One file holds everything → atomic restore from a backup is one
    file, not a tree walk.
  - The list is short (5-10 photos × few pets) so YAML is plenty fast.
  - It composes with the existing backup module — adding `pets.yml`
    to backup.INCLUDE is a one-liner future PR.

Pet IDs are deterministic slugs derived from name (lowercase, ascii,
underscored). The original display name is kept verbatim so users can
write "Mochi 麻糬" and we still get a safe directory path.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from .utils import atomic_write_text

logger = logging.getLogger("pawcorder.pets_store")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
PETS_YAML = DATA_DIR / "config" / "pets.yml"
PETS_PHOTO_DIR = DATA_DIR / "pets"

VALID_SPECIES = ("cat", "dog")
NAME_RE = re.compile(r"^[\w一-鿿][\w一-鿿 \-]{0,40}$")  # ascii word + CJK
SLUG_RE = re.compile(r"[^a-z0-9_]+")
MAX_PHOTOS_PER_PET = 30  # generous cap; UI will warn at ~10


@dataclass
class PetPhoto:
    """One reference image. `embedding` is L2-normalized.

    ``backbone`` records which embedding model was active when this
    photo was enrolled. Recognition uses it to skip embeddings from a
    different backbone — without this, switching from MobileNet (576-d)
    to DINOv2 (384-d) would silently start matching against vectors in
    a foreign feature space until the user re-enrolled.

    Older pets.yml files predate the field; `from_dict` defaults it to
    "mobilenetv3_small_100" which is what they were actually trained
    against — so a YAML migration isn't needed.
    """
    filename: str           # relative to PETS_PHOTO_DIR / pet_id / "photos"
    embedding: list[float]  # length = embeddings backbone's dim
    uploaded_at: int = 0    # unix seconds, informational
    backbone: str = "mobilenetv3_small_100"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PetPhoto":
        return PetPhoto(
            filename=str(d.get("filename", "")),
            embedding=[float(x) for x in (d.get("embedding") or [])],
            uploaded_at=int(d.get("uploaded_at") or 0),
            backbone=str(d.get("backbone") or "mobilenetv3_small_100"),
        )


@dataclass
class Pet:
    pet_id: str            # slug derived from name; immutable once created
    name: str              # display name; user can edit freely
    species: str = "cat"
    notes: str = ""
    photos: list[PetPhoto] = field(default_factory=list)
    # Per-pet cosine threshold from local calibration / remote fine-tune.
    # 0.0 means "not calibrated — use the global default". Stored
    # alongside the pet so backups carry it, and so a YAML-only edit
    # by the user can bump it manually if they have intuition for the
    # tradeoff. See pro/finetune.py for how it's chosen.
    match_threshold: float = 0.0
    # Latest calibration sweep result for transparency on the UI.
    calibration: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pet_id": self.pet_id,
            "name": self.name,
            "species": self.species,
            "notes": self.notes,
            "photos": [p.to_dict() for p in self.photos],
            "match_threshold": self.match_threshold,
            "calibration": self.calibration,
        }

    @staticmethod
    def from_dict(d: dict) -> "Pet":
        return Pet(
            pet_id=str(d.get("pet_id") or "").strip(),
            name=str(d.get("name") or "").strip(),
            species=str(d.get("species") or "cat").strip().lower(),
            notes=str(d.get("notes") or ""),
            photos=[PetPhoto.from_dict(p) for p in (d.get("photos") or []) if isinstance(p, dict)],
            match_threshold=float(d.get("match_threshold") or 0.0),
            calibration=dict(d.get("calibration") or {}),
        )


class PetValidationError(ValueError):
    pass


# ---- helpers -----------------------------------------------------------

def slugify(name: str) -> str:
    """Stable lowercase ASCII slug suitable for a directory name.

    'Mochi 麻糬' → 'mochi'. If the name is purely CJK ('麻糬'), we
    fall back to 'pet_<hex>' so the directory is still creatable.
    """
    s = name.strip().lower()
    s = SLUG_RE.sub("_", s)
    s = s.strip("_")
    if not s or not s[0].isascii():
        # Pure non-ASCII → derive a stable hash so two pets with
        # identical CJK names still get distinct slugs.
        import hashlib
        h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        s = f"pet_{h}"
    return s[:32]


def validate_name(name: str) -> None:
    if not NAME_RE.match(name or ""):
        raise PetValidationError(
            "Pet name may contain letters, digits, spaces, dashes, and CJK characters (max 40)."
        )


def validate_species(species: str) -> None:
    if species not in VALID_SPECIES:
        raise PetValidationError(f"species must be one of {VALID_SPECIES}, got {species!r}")


# ---- store ------------------------------------------------------------

class PetStore:
    """File-backed CRUD. Photos live next to pets.yml, never inside it.

    All write operations (``save_all``, ``add_photo``, ``remove_photo``,
    ``create``, ``update``, ``delete``) are serialised through a
    process-wide lock — re-enroll's read-modify-write loop runs for
    seconds and a concurrent ``add_photo`` from a different worker
    would otherwise lose its append when the slower writer's
    ``save_all`` lands second.
    """

    # Process-wide so any PetStore instance shares it. The store is
    # cheap to construct (no I/O until method call) and tests build
    # them ad-hoc, so a module-level lock is the right scope.
    _write_lock: threading.RLock = threading.RLock()

    def __init__(self, *, yaml_path: Path = PETS_YAML, photo_dir: Path = PETS_PHOTO_DIR) -> None:
        self.yaml_path = yaml_path
        self.photo_dir = photo_dir

    # --- read ---

    def load(self) -> list[Pet]:
        if not self.yaml_path.exists():
            return []
        try:
            data = yaml.safe_load(self.yaml_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            logger.warning("pets.yml is malformed; treating as empty")
            return []
        items = data.get("pets") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out: list[Pet] = []
        for entry in items:
            if isinstance(entry, dict) and entry.get("pet_id"):
                try:
                    out.append(Pet.from_dict(entry))
                except (TypeError, ValueError):
                    continue
        return out

    def get(self, pet_id: str) -> Pet | None:
        for p in self.load():
            if p.pet_id == pet_id:
                return p
        return None

    def names(self) -> list[str]:
        return [p.name for p in self.load()]

    # --- write ---

    def save_all(self, pets: list[Pet]) -> None:
        """Atomic — sees no torn writes if killed mid-flight. Lock-
        protected so a slow read-modify-write (re-enroll) can't race a
        concurrent ``add_photo`` and silently drop the new entry."""
        with self._write_lock:
            payload = {"pets": [p.to_dict() for p in pets]}
            atomic_write_text(
                self.yaml_path,
                yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            )

    def create(self, *, name: str, species: str, notes: str = "") -> Pet:
        validate_name(name)
        validate_species(species)
        with self._write_lock:
            pets = self.load()
            existing_ids = {p.pet_id for p in pets}
            existing_names = {p.name for p in pets}
            if name in existing_names:
                raise PetValidationError(f"a pet named {name!r} already exists")
            # Disambiguate slug if collision (rare — only happens with similar-looking names).
            base_slug = slugify(name) or "pet"
            slug = base_slug
            i = 2
            while slug in existing_ids:
                slug = f"{base_slug}_{i}"
                i += 1
            pet = Pet(pet_id=slug, name=name.strip(), species=species, notes=notes)
            pets.append(pet)
            self.save_all(pets)
        # Pre-create the photo dir so first upload doesn't race.
        (self.photo_dir / pet.pet_id / "photos").mkdir(parents=True, exist_ok=True)
        return pet

    def update(self, pet_id: str, *, name: str | None = None, species: str | None = None,
               notes: str | None = None) -> Pet:
        with self._write_lock:
            pets = self.load()
            target = next((p for p in pets if p.pet_id == pet_id), None)
            if not target:
                raise KeyError(pet_id)
            if name is not None:
                validate_name(name)
                other_names = {p.name for p in pets if p.pet_id != pet_id}
                if name in other_names:
                    raise PetValidationError(f"a pet named {name!r} already exists")
                target.name = name.strip()
            if species is not None:
                validate_species(species)
                target.species = species
            if notes is not None:
                target.notes = notes
            self.save_all(pets)
            return target

    def delete(self, pet_id: str) -> bool:
        with self._write_lock:
            pets = self.load()
            new = [p for p in pets if p.pet_id != pet_id]
            if len(new) == len(pets):
                return False
            self.save_all(new)
        # Best-effort photo cleanup. We don't fail the call if the dir
        # has weird permissions — the user can sweep manually.
        target_dir = self.photo_dir / pet_id
        if target_dir.exists():
            try:
                shutil.rmtree(target_dir)
            except OSError as exc:
                logger.warning("could not remove photo dir for %s: %s", pet_id, exc)
        return True

    # --- photos ---

    def add_photo(self, pet_id: str, image_bytes: bytes, embedding: Iterable[float],
                  *, ext: str = ".jpg", uploaded_at: int = 0,
                  backbone: Optional[str] = None) -> PetPhoto:
        """Persist photo bytes to disk + append a PetPhoto entry.

        ``backbone`` defaults to whichever embedding model is active
        right now — capturing it at write time means future recognition
        can skip embeddings from foreign backbones if the operator
        switches without re-enrolling.
        """
        with self._write_lock:
            pets = self.load()
            target = next((p for p in pets if p.pet_id == pet_id), None)
            if not target:
                raise KeyError(pet_id)
            if len(target.photos) >= MAX_PHOTOS_PER_PET:
                raise PetValidationError(
                    f"too many photos for {pet_id} (max {MAX_PHOTOS_PER_PET})"
                )
            photo_dir = self.photo_dir / pet_id / "photos"
            photo_dir.mkdir(parents=True, exist_ok=True)
            # Sequential filename so list order matches upload order.
            n = len(target.photos) + 1
            filename = f"photo_{n:02d}{ext}"
            out = photo_dir / filename
            out.write_bytes(image_bytes)
            try:
                os.chmod(out, 0o600)
            except (PermissionError, OSError):
                pass
            if backbone is None:
                from . import embeddings as _emb
                backbone = _emb.active_backbone_name()
            photo = PetPhoto(filename=filename, embedding=list(embedding),
                             uploaded_at=int(uploaded_at), backbone=backbone)
            target.photos.append(photo)
            self.save_all(pets)
            return photo

    def remove_photo(self, pet_id: str, filename: str) -> bool:
        with self._write_lock:
            pets = self.load()
            target = next((p for p in pets if p.pet_id == pet_id), None)
            if not target:
                return False
            before = len(target.photos)
            target.photos = [p for p in target.photos if p.filename != filename]
            if len(target.photos) == before:
                return False
            self.save_all(pets)
        path = self.photo_dir / pet_id / "photos" / filename
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return True

    def photo_path(self, pet_id: str, filename: str) -> Path:
        return self.photo_dir / pet_id / "photos" / filename
