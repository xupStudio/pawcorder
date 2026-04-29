"""Schema migrations for pawcorder's YAML / JSON configs.

Why we need this: cameras.yml and pets.yml gain new fields over
time. A user upgrading from v1 to v3 needs us to read their old file
and quietly add the new fields with sane defaults — without losing
their data.

Approach:
  - Each managed file has a top-level `schema_version: N` key.
  - Migrations are pure functions: `(old_dict) -> new_dict`.
  - On boot, run_all() walks every file, applies migrations
    sequentially up to the current schema, atomic-writes the result.
  - Idempotent: running twice is a no-op.

Today we ship migrations 1 → 2 (zones / privacy_masks for cameras,
notes for pets). Files written before this module existed have no
schema_version key — they're treated as version 0 and migrated up.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from .utils import atomic_write_text

logger = logging.getLogger("pawcorder.migrations")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))


@dataclass
class MigrationResult:
    file: str
    from_version: int
    to_version: int
    applied: list[int]
    error: str = ""


# ---- migration table ---------------------------------------------------

def _cameras_v0_to_v1(data: dict) -> dict:
    """v1 — establish the schema_version baseline. No field changes;
    just stamp the file so future migrators have a number to work
    against. The pre-this-module cameras.yml is already 'v1-shaped'."""
    data.setdefault("schema_version", 1)
    return data


def _cameras_v1_to_v2(data: dict) -> dict:
    """v2 — add zones / privacy_masks / audio_detection / ptz_presets
    fields per camera. Old files just get empty defaults; nothing the
    user did is lost."""
    data["schema_version"] = 2
    cameras = data.get("cameras") or []
    for cam in cameras:
        if not isinstance(cam, dict):
            continue
        cam.setdefault("zones", [])
        cam.setdefault("privacy_masks", [])
        cam.setdefault("audio_detection", False)
        cam.setdefault("ptz_presets", [])
    return data


def _pets_v0_to_v1(data: dict) -> dict:
    """v1 — initial schema. Like cameras, just stamp the version."""
    data.setdefault("schema_version", 1)
    return data


# (file → list of (target_version, migrator)) in sequence order.
# To add a new migration: drop another tuple at the end of the list.
MIGRATIONS: dict[str, list[tuple[int, Callable[[dict], dict]]]] = {
    "config/cameras.yml": [
        (1, _cameras_v0_to_v1),
        (2, _cameras_v1_to_v2),
    ],
    "config/pets.yml": [
        (1, _pets_v0_to_v1),
    ],
}


# ---- runner ------------------------------------------------------------

def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("could not parse %s: %s — leaving alone", path, exc)
        return {}
    if not isinstance(data, dict):
        # File was written as a plain list / scalar — skip; we don't
        # want to clobber whatever's there.
        return {}
    return data


def migrate_file(rel_path: str) -> MigrationResult:
    """Run all needed migrators for one file. Idempotent."""
    path = DATA_DIR / rel_path
    if not path.exists():
        # Nothing to migrate — but no error either.
        return MigrationResult(file=rel_path, from_version=0, to_version=0, applied=[])

    data = _read_yaml(path)
    if not data:
        return MigrationResult(file=rel_path, from_version=0, to_version=0, applied=[],
                               error="file empty or unparseable")

    current = int(data.get("schema_version") or 0)
    applied: list[int] = []
    starting_version = current
    for target_version, migrator in MIGRATIONS.get(rel_path, []):
        if current >= target_version:
            continue
        try:
            data = migrator(data)
            current = target_version
            applied.append(target_version)
        except Exception as exc:  # noqa: BLE001
            logger.warning("migration of %s to v%d failed: %s", rel_path, target_version, exc)
            return MigrationResult(
                file=rel_path, from_version=starting_version,
                to_version=current, applied=applied, error=str(exc),
            )

    if applied:
        try:
            atomic_write_text(
                path,
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            )
            logger.info("migrated %s: v%d -> v%d", rel_path, starting_version, current)
        except OSError as exc:
            return MigrationResult(
                file=rel_path, from_version=starting_version,
                to_version=current, applied=applied,
                error=f"write failed: {exc}",
            )

    return MigrationResult(
        file=rel_path, from_version=starting_version, to_version=current,
        applied=applied,
    )


def run_all() -> list[MigrationResult]:
    """Run every registered migration on boot. Called once from
    main.lifespan. Failures don't crash the app — they just leave
    that file at its current version."""
    return [migrate_file(rel) for rel in MIGRATIONS]


def current_versions() -> dict[str, int]:
    """For /system → diagnostics. Returns latest-target-version per file."""
    return {
        rel: chain[-1][0] if chain else 0
        for rel, chain in MIGRATIONS.items()
    }
