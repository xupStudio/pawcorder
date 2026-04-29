"""Configuration backup & restore.

If the host SSD dies, all the user has to do to recover is bring up a
fresh pawcorder host, restore the last backup, and Frigate is rolling
again — same cameras, same cloud, same passwords.

Backup is a single tar.gz containing:
    .env                    host-wide config (passwords, tokens, knobs)
    config/cameras.yml      camera list with credentials
    config/rclone/rclone.conf  cloud OAuth tokens / API keys
    config/frigate.template.yml   in case the user customized it
    config/config.yml       last rendered Frigate config (informational)
    pawcorder-backup.json   metadata: schema version, created_at, app version

We deliberately do NOT include /media/frigate (the recordings). Backups
are meant to be small (~kilobytes) so they fit in any cloud storage.

Restore validates the schema version before extracting, and writes files
atomically via a temp directory so a partial restore can't corrupt the
host's working config.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
BACKUP_VERSION = 1
META_FILENAME = "pawcorder-backup.json"

# Files included in a backup, in priority order. Paths are relative to DATA_DIR.
INCLUDE = [
    ".env",
    "config/cameras.yml",
    "config/rclone/rclone.conf",
    "config/frigate.template.yml",
    "config/config.yml",
]


@dataclass
class BackupResult:
    ok: bool
    bytes_written: int = 0
    files_included: int = 0
    error: str = ""


@dataclass
class RestoreResult:
    ok: bool
    files_restored: int = 0
    error: str = ""
    schema_version: int = 0


def make_backup(data_dir: Path | None = None) -> bytes:
    """Build a tar.gz of the user's pawcorder config in memory.

    Returns the raw bytes so the caller can stream them as an HTTP
    download. Empty/missing files are skipped silently — a fresh install
    won't have a rclone.conf yet, that's fine.
    """
    base = Path(data_dir) if data_dir else DATA_DIR
    buf = io.BytesIO()
    files_included = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = {
            "version": BACKUP_VERSION,
            "created_at": int(time.time()),
            "app": "pawcorder",
        }
        meta_bytes = json.dumps(meta, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name=META_FILENAME)
        info.size = len(meta_bytes)
        info.mtime = int(time.time())
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(meta_bytes))

        for rel in INCLUDE:
            src = base / rel
            if not src.exists() or not src.is_file():
                continue
            try:
                data = src.read_bytes()
            except OSError:
                continue
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            info.mtime = int(src.stat().st_mtime)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
            files_included += 1

    return buf.getvalue()


def inspect_backup(blob: bytes) -> dict:
    """Read the metadata header of a backup blob without extracting it.

    Returns a dict including a `compatible` flag so the UI can red-flag
    a backup before the user clicks Restore (rather than after the
    server returns 400).

    Raises ValueError on malformed input.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            members = tar.getmembers()
            names = [m.name for m in members if m.isfile()]
            meta_member = next((m for m in members if m.name == META_FILENAME), None)
            if meta_member is None:
                raise ValueError("not a pawcorder backup (missing metadata)")
            f = tar.extractfile(meta_member)
            if f is None:
                raise ValueError("metadata unreadable")
            meta = json.loads(f.read().decode("utf-8"))
            version = int(meta.get("version") or 0)
            return {
                "version": version,
                "compatible": version == BACKUP_VERSION,
                "expected_version": BACKUP_VERSION,
                "created_at": int(meta.get("created_at") or 0),
                "app": meta.get("app", ""),
                "files": [n for n in names if n != META_FILENAME],
            }
    except (tarfile.TarError, json.JSONDecodeError) as exc:
        raise ValueError(f"backup is corrupt: {exc}") from exc


def restore_backup(blob: bytes, data_dir: Path | None = None) -> RestoreResult:
    """Validate and extract a backup blob to data_dir.

    The pawcorder service should be stopped before calling this so we
    don't fight a live process for the same files. We don't enforce that
    here — the route layer does.
    """
    base = Path(data_dir) if data_dir else DATA_DIR
    try:
        meta = inspect_backup(blob)
    except ValueError as exc:
        return RestoreResult(ok=False, error=str(exc))

    if meta["version"] != BACKUP_VERSION:
        return RestoreResult(
            ok=False,
            schema_version=meta["version"],
            error=f"backup schema v{meta['version']} not supported (this build expects v{BACKUP_VERSION})",
        )

    # Sanity-check member paths so a malicious tarball can't escape data_dir.
    safe_files: list[tarfile.TarInfo] = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name == META_FILENAME:
                continue
            if not m.isfile():
                continue
            normalized = os.path.normpath(m.name)
            if normalized.startswith("..") or os.path.isabs(normalized):
                return RestoreResult(ok=False, error=f"unsafe path in backup: {m.name!r}")
            if normalized not in INCLUDE:
                return RestoreResult(ok=False, error=f"unexpected file in backup: {m.name!r}")
            safe_files.append(m)

        # Extract one by one. We write atomically: data → temp → rename.
        for m in safe_files:
            f = tar.extractfile(m)
            if f is None:
                continue
            payload = f.read()
            dst = base / m.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".restoring")
            tmp.write_bytes(payload)
            try:
                os.chmod(tmp, 0o600)
            except PermissionError:
                pass
            os.replace(tmp, dst)

    return RestoreResult(ok=True, files_restored=len(safe_files), schema_version=meta["version"])


def humanize_bytes(n: int) -> str:
    """Compact byte formatter used in UI tooltips."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
