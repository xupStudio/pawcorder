"""Storage candidates for the setup wizard — clickable cards instead of a path field.

The admin runs inside Docker, so we only see filesystems exposed via the
container's /proc/mounts (the project bind-mount, NAS shares the operator
pre-mounted, anything wired into a custom compose). Best-effort nudge,
not a complete host inventory.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("pawcorder.storage_detect")

# Filesystem types that aren't real storage. We skip them when parsing
# /proc/mounts so the candidate list isn't dominated by container
# overlays and kernel pseudo-filesystems.
_VIRTUAL_FS = frozenset({
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2",
    "overlay", "overlay2", "aufs", "fuse.lxcfs", "binfmt_misc", "mqueue",
    "pstore", "tracefs", "debugfs", "securityfs", "ramfs", "rpc_pipefs",
    "configfs", "fusectl", "squashfs", "autofs", "bpf",
})

# Mount points that are real but never useful as storage targets.
_SKIP_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/etc", "/var/run",
                  "/var/lib/docker", "/snap", "/boot")

DEFAULT_PATH = "/mnt/pawcorder"

# Candidate paths we always probe for free space. If the path exists and
# is readable, we surface it. Anyone who pre-mounted a USB or NAS into a
# common location gets a one-click option.
_COMMON_CANDIDATES = (
    "/mnt/pawcorder",
    "/mnt/storage",
    "/mnt/usb",
    "/srv/pawcorder",
    "/var/lib/pawcorder",
)

# Below this we don't bother showing the candidate — too small to record
# any meaningful video into.
_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB


@dataclass
class StorageCandidate:
    path: str
    total_bytes: int
    free_bytes: int
    label: str = ""              # "USB", "NAS", "Local disk" — optional badge
    is_default: bool = False     # marks the recommended path
    exists: bool = True

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "label": self.label,
            "is_default": self.is_default,
            "exists": self.exists,
        }


_MOUNTS_PATH = Path("/proc/mounts")


def _read_mounts(source: Path | None = None) -> list[tuple[str, str]]:
    """Return [(mount_point, fs_type), ...] from /proc/mounts.

    Empty list on non-Linux hosts or when the file is unreadable
    (running tests on macOS, sandboxed envs). Callers fall back to the
    common-candidate probe.
    """
    proc = source if source is not None else _MOUNTS_PATH
    if not proc.exists():
        return []
    try:
        text = proc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fs_type = parts[1], parts[2]
        if fs_type in _VIRTUAL_FS:
            continue
        if any(mount_point == p or mount_point.startswith(p + "/") for p in _SKIP_PREFIXES):
            continue
        out.append((mount_point, fs_type))
    return out


def _label_for(mount_point: str, fs_type: str) -> str:
    """Heuristic badge so the wizard can render "USB" / "NAS" tags."""
    fs = fs_type.lower()
    mp = mount_point.lower()
    if fs in ("nfs", "nfs4", "cifs", "smbfs", "smb3"):
        return "NAS"
    if "usb" in mp or "media" in mp:
        return "USB"
    if mp in ("/", "/data"):
        return "Local disk"
    return ""


def _probe(path: str) -> tuple[int, int] | None:
    """Return (total, free) bytes for path. None if not accessible."""
    try:
        usage = shutil.disk_usage(path)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return usage.total, usage.free


def detect_candidates(current_path: str | None = None) -> list[StorageCandidate]:
    """Return a deduped list of candidate paths sorted by free space desc.

    The default `/mnt/pawcorder` is always present, even if it doesn't
    exist yet — the user will create it (or rely on the installer to)
    when they pick it. ``current_path`` is included if set so the user
    can confirm what they already have.
    """
    seen: set[str] = set()
    candidates: list[StorageCandidate] = []

    def _add(path: str, label: str = "", is_default: bool = False) -> None:
        path = path.rstrip("/") or "/"
        if path in seen:
            return
        seen.add(path)
        probed = _probe(path)
        if probed is None:
            candidates.append(StorageCandidate(
                path=path, total_bytes=0, free_bytes=0,
                label=label, is_default=is_default, exists=False,
            ))
            return
        total, free = probed
        if not is_default and free < _MIN_FREE_BYTES:
            return
        candidates.append(StorageCandidate(
            path=path, total_bytes=total, free_bytes=free,
            label=label, is_default=is_default, exists=True,
        ))

    _add(DEFAULT_PATH, label="Recommended", is_default=True)
    if current_path and current_path != DEFAULT_PATH:
        _add(current_path, label="Current")
    for mp, fs in _read_mounts():
        _add(mp, label=_label_for(mp, fs))
    for path in _COMMON_CANDIDATES:
        _add(path)

    # Sort: default first, then current, then by free space desc, then
    # by path for stable ordering when nothing has free space data.
    def _key(c: StorageCandidate) -> tuple:
        return (
            0 if c.is_default else (1 if c.label == "Current" else 2),
            -c.free_bytes,
            c.path,
        )

    candidates.sort(key=_key)
    return candidates
