"""Daily auto-backup to the user's connected cloud, with optional
password-based encryption.

Pipeline once per day at BACKUP_HOUR:
  1. Run backup.make_backup() to get the tar.gz blob.
  2. If encryption is enabled, AES-GCM-encrypt with a key derived
     from the user's BACKUP_ENCRYPTION_PASSWORD via PBKDF2.
  3. Push to cloud via rclone (`rclone rcat` so we don't write a
     temp file on the host disk).
  4. Track last-success timestamp + last-error in config so the
     /backup page can show "last cloud backup: 4 hours ago".

Encryption format (compact, single file):
    pwc-bkp-v1\n         (8-byte magic + version)
    <16-byte salt>
    <12-byte nonce>
    <ciphertext + auth tag>

PBKDF2-HMAC-SHA256, 200_000 iterations, 32-byte key. AES-256-GCM.
We don't ship a "decrypt with password" CLI — the user can decrypt
in admin's /backup page (which understands this format) or with any
standard AES-GCM tool.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import backup as backup_mod, cloud, config_store

logger = logging.getLogger("pawcorder.backup_schedule")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
SCHEDULE_PATH = DATA_DIR / "config" / "backup_schedule.json"
BACKUP_HOUR = int(os.environ.get("PAWCORDER_BACKUP_HOUR", "3"))
LOOP_CHECK_INTERVAL = 600  # 10 min — coarse poll like highlights
MAGIC = b"pwc-bkp-v1\n"


@dataclass
class ScheduleState:
    enabled: bool = False
    encrypt: bool = False
    encryption_password: str = ""        # if set, derive key per backup
    cloud_path: str = "pawcorder/backups" # subdir inside the cloud remote
    last_run_ok_at: int = 0
    last_run_attempt_at: int = 0
    last_error: str = ""

    def to_dict(self, *, include_password: bool = False) -> dict:
        d = {
            "enabled": self.enabled,
            "encrypt": self.encrypt,
            "cloud_path": self.cloud_path,
            "last_run_ok_at": self.last_run_ok_at,
            "last_run_attempt_at": self.last_run_attempt_at,
            "last_error": self.last_error,
        }
        if include_password:
            d["encryption_password"] = self.encryption_password
        else:
            d["password_set"] = bool(self.encryption_password)
        return d


# ---- state IO ----------------------------------------------------------

def load_state() -> ScheduleState:
    if not SCHEDULE_PATH.exists():
        return ScheduleState()
    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ScheduleState()
    return ScheduleState(
        enabled=bool(data.get("enabled", False)),
        encrypt=bool(data.get("encrypt", False)),
        encryption_password=str(data.get("encryption_password", "")),
        cloud_path=str(data.get("cloud_path") or "pawcorder/backups"),
        last_run_ok_at=int(data.get("last_run_ok_at") or 0),
        last_run_attempt_at=int(data.get("last_run_attempt_at") or 0),
        last_error=str(data.get("last_error") or ""),
    )


def save_state(state: ScheduleState) -> None:
    from .utils import atomic_write_text
    atomic_write_text(SCHEDULE_PATH, json.dumps(state.to_dict(include_password=True), indent=2))


# ---- encryption --------------------------------------------------------

def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000, dklen=32)


def encrypt_blob(plaintext: bytes, password: str) -> bytes:
    """AES-256-GCM. Output: MAGIC + salt + nonce + ciphertext+tag."""
    if not password:
        raise ValueError("password required")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RuntimeError("cryptography not installed — encrypt unavailable")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive_key(password, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return MAGIC + salt + nonce + ct


def decrypt_blob(blob: bytes, password: str) -> bytes:
    """Inverse of encrypt_blob. Raises ValueError on bad password /
    tampering / wrong magic."""
    if not blob.startswith(MAGIC):
        raise ValueError("not a pawcorder encrypted backup")
    body = blob[len(MAGIC):]
    if len(body) < 16 + 12 + 16:
        raise ValueError("backup truncated")
    salt, nonce, ct = body[:16], body[16:28], body[28:]
    key = _derive_key(password, salt)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception as exc:  # noqa: BLE001 — broad on purpose, hide which step failed
        raise ValueError("decryption failed (wrong password or corrupt blob)") from exc


# ---- cloud push --------------------------------------------------------

async def _rclone_rcat(remote_path: str, blob: bytes, *, timeout: int = 120) -> tuple[bool, str]:
    """Stream `blob` to `<remote>:<remote_path>` via stdin. Returns
    (ok, error_detail). Doesn't touch disk on the host."""
    cmd = [cloud.RCLONE_BIN, "--config", str(cloud.RCLONE_CONFIG_PATH),
           "rcat", remote_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=blob), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "rclone rcat timed out"
        if proc.returncode != 0:
            return False, (stderr or b"").decode("utf-8", errors="replace")[-300:]
        return True, ""
    except FileNotFoundError:
        return False, "rclone binary missing"


# ---- run ---------------------------------------------------------------

async def run_once_now() -> dict:
    """Trigger a backup attempt right now, regardless of time-of-day.
    Returns a result dict for the UI."""
    state = load_state()
    state.last_run_attempt_at = int(time.time())
    save_state(state)

    if not state.enabled:
        state.last_error = "scheduled backup is disabled"
        save_state(state)
        return {"ok": False, "error": state.last_error}

    cfg = config_store.load_config()
    remote = cfg.cloud_remote_name
    if not remote or remote not in cloud.list_remotes():
        state.last_error = "no cloud remote configured"
        save_state(state)
        return {"ok": False, "error": state.last_error}

    blob = backup_mod.make_backup()
    if state.encrypt:
        if not state.encryption_password:
            state.last_error = "encryption enabled but no password set"
            save_state(state)
            return {"ok": False, "error": state.last_error}
        try:
            blob = encrypt_blob(blob, state.encryption_password)
        except RuntimeError as exc:
            state.last_error = str(exc)
            save_state(state)
            return {"ok": False, "error": state.last_error}

    fname = time.strftime("pawcorder-backup-%Y%m%dT%H%M%S")
    fname += ".enc.bin" if state.encrypt else ".tar.gz"
    remote_path = f"{remote}:{state.cloud_path.rstrip('/')}/{fname}"

    ok, err = await _rclone_rcat(remote_path, blob)
    if not ok:
        state.last_error = err or "rclone failed"
        save_state(state)
        return {"ok": False, "error": state.last_error}

    state.last_run_ok_at = int(time.time())
    state.last_error = ""
    save_state(state)
    return {"ok": True, "remote_path": remote_path, "bytes": len(blob)}


# ---- background scheduler ---------------------------------------------

class BackupScheduler:
    """Daily run at BACKUP_HOUR local. Idempotent — won't run twice
    in the same calendar day."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_built_date: str = ""

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="backup-scheduler")
            logger.info("backup scheduler started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("backup tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=LOOP_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        state = load_state()
        if not state.enabled:
            return
        now = time.localtime()
        if now.tm_hour < BACKUP_HOUR:
            return
        date_label = time.strftime("%Y-%m-%d", now)
        if self._last_built_date == date_label:
            return
        self._last_built_date = date_label
        result = await run_once_now()
        if result.get("ok"):
            logger.info("backup pushed for %s", date_label)
        else:
            logger.warning("backup failed for %s: %s", date_label, result.get("error"))


scheduler = BackupScheduler()
