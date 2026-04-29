"""Uninstall — inventory + in-place reset + command generator.

Three reasonable mental models a user might have:

  1. "I want to start over without losing recordings."
     → reset_app_data(): wipe cameras.yml / pets.yml / sightings /
       privacy / rclone / embedding model. KEEP .env (so admin
       password survives), KEEP recordings at STORAGE_PATH. The
       admin keeps running, setup wizard appears on next page load.

  2. "I want pawcorder gone but keep my pet videos."
     → uninstall_command(level='full'): a shell command the user
       runs on the host. We can't reliably do this from inside
       the admin because the last step kills the admin itself.

  3. "Wipe everything, including recordings."
     → uninstall_command(level='nuke'): same script, with the
       extra rm -rf STORAGE_PATH at the end.

The inventory function lists what's currently on disk, with sizes,
so the UI can show "your recordings take up 47 GB" before the user
hits Nuke.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config_store, docker_ops

logger = logging.getLogger("pawcorder.uninstall")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))

# Files / dirs we know we created, grouped by category. Each item is
# (relative_path, label_key) — label_key resolves to a user-friendly
# string in the UI via i18n.
_APP_DATA_PATHS: list[tuple[str, str]] = [
    ("config/cameras.yml",        "UNINSTALL_PATH_CAMERAS"),
    ("config/pets.yml",           "UNINSTALL_PATH_PETS"),
    ("config/sightings.ndjson",   "UNINSTALL_PATH_SIGHTINGS"),
    ("config/privacy.json",       "UNINSTALL_PATH_PRIVACY"),
    ("config/rclone/rclone.conf", "UNINSTALL_PATH_RCLONE"),
    ("config/email_signups.csv",  "UNINSTALL_PATH_SIGNUPS"),
    ("config/config.yml",         "UNINSTALL_PATH_FRIGATE_RENDERED"),
    # Newer features — credentials / state that must also vanish on
    # a reset, otherwise resetting leaves leftover secrets behind.
    ("config/api_keys.json",       "UNINSTALL_PATH_API_KEYS"),
    ("config/webpush_vapid.json",  "UNINSTALL_PATH_WEBPUSH_VAPID"),
    ("config/webpush_subs.json",   "UNINSTALL_PATH_WEBPUSH_SUBS"),
    ("config/backup_schedule.json","UNINSTALL_PATH_BACKUP_SCHEDULE"),
    ("config/energy_mode.json",    "UNINSTALL_PATH_ENERGY_MODE"),
    # G-series additions (multi-user + NAS mount). users.yml holds
    # password hashes; SMB creds file (typically /etc/pawcorder.smbcreds)
    # is owned by root and listed separately because it sits outside DATA_DIR.
    ("config/users.yml",           "UNINSTALL_PATH_USERS"),
]
# Files we manage outside DATA_DIR. They're absolute paths and only
# get reset if they exist + the test environment can write to them.
_APP_HOST_PATHS: list[tuple[str, str]] = [
    ("/etc/pawcorder.smbcreds", "UNINSTALL_PATH_SMBCREDS"),
]
_APP_DATA_DIRS: list[tuple[str, str]] = [
    ("pets",            "UNINSTALL_PATH_PET_PHOTOS"),
    ("models",          "UNINSTALL_PATH_MODELS"),
    # Per-camera 30-day heatmap cache PNGs.
    ("config/heatmaps", "UNINSTALL_PATH_HEATMAPS"),
]
# .env is special — holds ADMIN_PASSWORD/SECRET; we DON'T wipe it on
# the soft reset, we just rewrite it to defaults except those two.
_ENV_PRESERVE_KEYS = ("ADMIN_PASSWORD", "ADMIN_SESSION_SECRET")


@dataclass
class FileEntry:
    """One line in the inventory — present or not, how big."""
    path: str             # absolute on host (or container)
    label_key: str        # i18n key
    exists: bool
    size_bytes: int = 0
    is_directory: bool = False


@dataclass
class ContainerEntry:
    name: str
    running: bool
    image: str = ""


@dataclass
class Inventory:
    """Snapshot of everything pawcorder owns or runs on this host."""
    config_files: list[FileEntry] = field(default_factory=list)
    config_dirs: list[FileEntry] = field(default_factory=list)
    env_file: Optional[FileEntry] = None
    storage_path: Optional[FileEntry] = None  # the recordings dir — usually huge
    containers: list[ContainerEntry] = field(default_factory=list)

    def total_app_data_bytes(self) -> int:
        """Sum of config + dirs (NOT recordings — those are listed separately
        because they're often orders of magnitude larger)."""
        s = 0
        for e in (*self.config_files, *self.config_dirs):
            s += e.size_bytes
        if self.env_file and self.env_file.exists:
            s += self.env_file.size_bytes
        return s

    def to_dict(self) -> dict:
        return {
            "config_files": [vars(e) for e in self.config_files],
            "config_dirs": [vars(e) for e in self.config_dirs],
            "env_file": vars(self.env_file) if self.env_file else None,
            "storage_path": vars(self.storage_path) if self.storage_path else None,
            "containers": [vars(c) for c in self.containers],
            "total_app_data_bytes": self.total_app_data_bytes(),
        }


# ---- size helpers ------------------------------------------------------

def _du(path: Path) -> int:
    """Recursive size in bytes. Returns 0 on missing / inaccessible."""
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                fp = Path(root) / f
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


# ---- inventory ---------------------------------------------------------

def take_inventory() -> Inventory:
    """Snapshot of all pawcorder-managed paths + containers + storage."""
    inv = Inventory()

    for rel, label in _APP_DATA_PATHS:
        p = DATA_DIR / rel
        inv.config_files.append(FileEntry(
            path=str(p), label_key=label,
            exists=p.exists(), size_bytes=_du(p), is_directory=False,
        ))

    for rel, label in _APP_DATA_DIRS:
        p = DATA_DIR / rel
        inv.config_dirs.append(FileEntry(
            path=str(p), label_key=label,
            exists=p.exists(), size_bytes=_du(p), is_directory=True,
        ))

    # Host-level credential files (outside DATA_DIR).
    for abs_path, label in _APP_HOST_PATHS:
        p = Path(abs_path)
        inv.config_files.append(FileEntry(
            path=str(p), label_key=label,
            exists=p.exists(), size_bytes=_du(p), is_directory=False,
        ))

    env_path = DATA_DIR / ".env"
    inv.env_file = FileEntry(
        path=str(env_path), label_key="UNINSTALL_PATH_ENV",
        exists=env_path.exists(), size_bytes=_du(env_path), is_directory=False,
    )

    # Recordings — read STORAGE_PATH from current config.
    cfg = config_store.load_config()
    if cfg.storage_path:
        sp = Path(cfg.storage_path)
        inv.storage_path = FileEntry(
            path=str(sp), label_key="UNINSTALL_PATH_RECORDINGS",
            exists=sp.exists(), size_bytes=_du(sp), is_directory=True,
        )

    # Containers — best-effort. If docker socket isn't mounted (test
    # mode), we just return an empty list.
    for cname in ("pawcorder-admin", "pawcorder-frigate", "pawcorder-watchtower"):
        try:
            client = docker_ops._client()  # noqa: SLF001
            c = client.containers.get(cname)
            state = (c.attrs.get("State") or {})
            inv.containers.append(ContainerEntry(
                name=cname,
                running=bool(state.get("Running")),
                image=c.image.tags[0] if c.image and c.image.tags else "",
            ))
        except Exception:  # noqa: BLE001
            inv.containers.append(ContainerEntry(name=cname, running=False))

    return inv


# ---- in-place soft reset (admin survives) -----------------------------

@dataclass
class ResetResult:
    ok: bool
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str = ""


def reset_app_data() -> ResetResult:
    """The soft path: wipe user data files but keep .env's auth bits and
    keep recordings. Admin keeps running and shows the setup wizard on
    next request.

    What we DON'T touch:
      - STORAGE_PATH (recordings)
      - .env's ADMIN_PASSWORD + ADMIN_SESSION_SECRET (so the user is
        not kicked out mid-click)
      - Docker containers / images (those need an external script)
    """
    result = ResetResult(ok=True)

    # 1. Remove the per-feature config files.
    for rel, _label in _APP_DATA_PATHS:
        p = DATA_DIR / rel
        if not p.exists():
            result.skipped.append(str(p))
            continue
        try:
            p.unlink()
            result.removed.append(str(p))
        except OSError as exc:
            logger.warning("could not remove %s: %s", p, exc)
            result.skipped.append(str(p))

    # 1b. Host-level credential files (live outside DATA_DIR — typically
    #     /etc/pawcorder.smbcreds). Best-effort: a non-root admin running
    #     in a container won't have permission, in which case we skip.
    for abs_path, _label in _APP_HOST_PATHS:
        p = Path(abs_path)
        if not p.exists():
            result.skipped.append(abs_path)
            continue
        try:
            p.unlink()
            result.removed.append(abs_path)
        except OSError as exc:
            logger.warning("could not remove %s: %s", p, exc)
            result.skipped.append(abs_path)

    # 2. Remove the per-feature directories (pet photos, embedding model).
    for rel, _label in _APP_DATA_DIRS:
        p = DATA_DIR / rel
        if not p.exists():
            result.skipped.append(str(p))
            continue
        try:
            shutil.rmtree(p)
            result.removed.append(str(p))
        except OSError as exc:
            logger.warning("could not remove %s: %s", p, exc)
            result.skipped.append(str(p))

    # 3. Rewrite .env to defaults BUT keep ADMIN_PASSWORD/SECRET so the
    #    user isn't logged out by their own click.
    try:
        current = config_store.read_env()
        preserved = {k: current.get(k, "") for k in _ENV_PRESERVE_KEYS}
        defaults = dict(config_store.DEFAULTS)
        defaults.update(preserved)
        config_store.write_env(defaults)
        result.removed.append(f"{DATA_DIR / '.env'} (reset to defaults, password kept)")
    except OSError as exc:
        result.ok = False
        result.error = f"failed to reset .env: {exc}"

    return result


# ---- shell command generator ------------------------------------------

def uninstall_command(level: str, *, project_dir: str = "~/pawcorder") -> str:
    """Return a shell command the user can paste on the host to finish
    the uninstall.

    Levels:
      'soft' — stop containers + remove images. Keeps project dir,
               keeps recordings, keeps config. Useful if the user
               wants to come back later.
      'full' — soft + remove project dir (config wiped). Recordings
               at STORAGE_PATH are KEPT.
      'nuke' — full + delete recordings.

    We deliberately don't pipe these through a single endpoint — the
    user gets a printable command, can review it before pasting,
    and can run it on their own schedule. There is no "clicking a
    button in the admin to delete the admin" because the admin would
    die mid-response.
    """
    cfg = config_store.load_config()
    storage = cfg.storage_path or "/mnt/pawcorder"

    if level == "soft":
        return (
            f"cd {project_dir} && "
            f"docker compose down && "
            f"docker rmi pawcorder/admin:local "
            f"ghcr.io/blakeblackshear/frigate:stable "
            f"containrrr/watchtower:1.7.1 2>/dev/null"
        )
    if level == "full":
        return (
            f"cd {project_dir} && "
            f"docker compose down -v && "
            f"docker rmi pawcorder/admin:local "
            f"ghcr.io/blakeblackshear/frigate:stable "
            f"containrrr/watchtower:1.7.1 2>/dev/null; "
            f"cd .. && rm -rf {project_dir}"
        )
    if level == "nuke":
        return (
            f"cd {project_dir} && "
            f"docker compose down -v && "
            f"docker rmi pawcorder/admin:local "
            f"ghcr.io/blakeblackshear/frigate:stable "
            f"containrrr/watchtower:1.7.1 2>/dev/null; "
            f"cd .. && rm -rf {project_dir} && "
            f"rm -rf {storage!r}"
        )
    raise ValueError(f"unknown level: {level!r}")


def humanize_bytes(n: int) -> str:
    """Compact size formatter for the inventory UI."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"
