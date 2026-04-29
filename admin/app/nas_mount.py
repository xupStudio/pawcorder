"""NAS mount helper — wraps mount/umount + /etc/fstab edit.

Two protocols supported (the only two that matter for home NAS):
  - NFS  (TrueNAS / OMV / Synology)
  - SMB  (Synology / QNAP / Windows shares)

Flow:
  1. test_mount(): try a one-shot mount in a tmp dir, report success
     or the specific error (perms, share path, credentials).
  2. install_mount(): on success, append a line to /etc/fstab (or a
     .pawcorder.mount file if we don't want to touch /etc) and run
     `mount` so the storage path is live now.

This module shells out — Python doesn't have an `nfs` library, so we
rely on `mount.nfs` / `mount.cifs` being installed (they are by
default on Ubuntu / Debian).

Soft-fails everywhere: routes that call us either get a usable
result or a friendly error, never a 500.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pawcorder.nas_mount")

VALID_PROTOCOLS = ("nfs", "smb")
FSTAB_PATH = Path(os.environ.get("PAWCORDER_FSTAB", "/etc/fstab"))
# Marker comment so we can find/replace pawcorder's lines without
# corrupting unrelated /etc/fstab entries.
FSTAB_MARKER = "# pawcorder-nas-mount"


@dataclass
class MountTestResult:
    ok: bool
    message: str = ""
    output: str = ""        # stderr from mount command on failure


@dataclass
class MountConfig:
    protocol: str           # 'nfs' or 'smb'
    server: str             # 192.168.1.10
    share: str              # /volume1/cameras (NFS) or //volume1/cameras (SMB)
    mount_point: str        # /mnt/pawcorder
    username: str = ""      # SMB only
    password: str = ""      # SMB only — stored in a secret credentials file


def validate(cfg: MountConfig) -> Optional[str]:
    if cfg.protocol not in VALID_PROTOCOLS:
        return f"protocol must be one of {VALID_PROTOCOLS}"
    if not cfg.server.strip():
        return "server is required"
    if not cfg.share.strip():
        return "share path is required"
    if not cfg.mount_point.startswith("/"):
        return "mount point must be an absolute path"
    if cfg.protocol == "smb" and not cfg.username:
        return "SMB needs at least a username (use 'guest' for unauth)"
    return None


def _has_command(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ---- one-shot test mount ----------------------------------------------

async def test_mount(cfg: MountConfig, *, timeout: int = 15) -> MountTestResult:
    """Mount to a tmp dir, list the contents, unmount. Surface any
    error verbatim so the user can fix typos / permissions."""
    err = validate(cfg)
    if err:
        return MountTestResult(ok=False, message=err)
    if cfg.protocol == "nfs" and not _has_command("mount.nfs"):
        return MountTestResult(ok=False, message="mount.nfs missing — run: sudo apt install nfs-common")
    if cfg.protocol == "smb" and not _has_command("mount.cifs"):
        return MountTestResult(ok=False, message="mount.cifs missing — run: sudo apt install cifs-utils")

    # We use a tmp directory inside the host's /tmp via mount —
    # if it works we get a populated dir, then unmount + clean up.
    tmp = Path(tempfile.mkdtemp(prefix="pawcorder-mount-test-"))
    try:
        if cfg.protocol == "nfs":
            cmd = ["mount", "-t", "nfs", "-o", "ro,soft,timeo=50",
                   f"{cfg.server}:{cfg.share}", str(tmp)]
        else:  # smb
            cmd = ["mount", "-t", "cifs", "-o",
                   f"username={cfg.username},password={cfg.password},ro,soft",
                   f"//{cfg.server}{cfg.share if cfg.share.startswith('/') else '/' + cfg.share}",
                   str(tmp)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return MountTestResult(ok=False, message=f"mount timed out after {timeout}s")
        if proc.returncode != 0:
            err_text = (stderr or b"").decode("utf-8", errors="replace")[-300:]
            return MountTestResult(ok=False, message="mount failed", output=err_text)
        # Probe — list a few entries.
        try:
            entries = sorted([p.name for p in tmp.iterdir()])[:5]
            sample = ", ".join(entries) if entries else "(empty share)"
        except OSError as exc:
            sample = f"could not list mounted dir: {exc}"
        # Best-effort unmount.
        await asyncio.create_subprocess_exec("umount", str(tmp),
                                             stdout=asyncio.subprocess.DEVNULL,
                                             stderr=asyncio.subprocess.DEVNULL)
        return MountTestResult(ok=True, message=f"mount OK, top entries: {sample}")
    finally:
        try:
            tmp.rmdir()
        except OSError:
            pass


# ---- fstab management --------------------------------------------------

def _strip_existing_pawcorder_lines(fstab_text: str) -> str:
    """Remove any prior pawcorder-managed mount lines so we can
    re-write idempotently."""
    out: list[str] = []
    skip_next = False
    for line in fstab_text.splitlines():
        if FSTAB_MARKER in line:
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        out.append(line)
    return "\n".join(out)


def install_to_fstab(cfg: MountConfig, *, smb_credentials_path: str = "/etc/pawcorder.smbcreds") -> Optional[str]:
    """Append a fstab entry. Returns None on success, error message
    on failure. We stamp our marker comment so a re-run replaces
    cleanly. SMB password lands in a 0600 credentials file (we never
    put it in fstab).
    """
    err = validate(cfg)
    if err:
        return err
    if not FSTAB_PATH.exists():
        return f"{FSTAB_PATH} not found"
    try:
        text = FSTAB_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return f"could not read {FSTAB_PATH}: {exc}"

    cleaned = _strip_existing_pawcorder_lines(text).rstrip() + "\n\n"
    cleaned += FSTAB_MARKER + " (do not edit by hand)\n"
    if cfg.protocol == "nfs":
        cleaned += (f"{cfg.server}:{cfg.share}  {cfg.mount_point}  "
                    f"nfs  defaults,_netdev,soft,timeo=50  0  0\n")
    else:
        # SMB: write creds file, reference it by path.
        creds = Path(smb_credentials_path)
        try:
            creds.write_text(
                f"username={cfg.username}\npassword={cfg.password}\n",
                encoding="utf-8",
            )
            os.chmod(creds, 0o600)
        except OSError as exc:
            return f"could not write SMB credentials file: {exc}"
        share = cfg.share if cfg.share.startswith("/") else "/" + cfg.share
        cleaned += (f"//{cfg.server}{share}  {cfg.mount_point}  cifs  "
                    f"credentials={creds},_netdev,iocharset=utf8,uid=1000,gid=1000  0  0\n")
    try:
        FSTAB_PATH.write_text(cleaned, encoding="utf-8")
    except OSError as exc:
        return f"could not write {FSTAB_PATH}: {exc}"
    return None


async def mount_now(mount_point: str) -> tuple[bool, str]:
    """Run `mount <point>` so the just-added fstab entry takes effect
    without a reboot."""
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "mount", mount_point,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "mount timed out"
    if proc.returncode != 0:
        return False, (stderr or b"").decode("utf-8", errors="replace")[-300:]
    return True, ""
