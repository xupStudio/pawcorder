"""Cloud backup via rclone (MIT license, multi-cloud).

The admin container ships with the rclone binary. This module is a thin
wrapper around it: read/write rclone.conf, run `rclone copy` to upload
event clips from Frigate to the user's chosen cloud.

Supported backends (subset of rclone's full list, picked for breadth):
  - drive    Google Drive
  - dropbox  Dropbox
  - onedrive Microsoft OneDrive
  - b2       Backblaze B2
  - s3       AWS S3 / R2 / Wasabi / etc.
  - webdav   WebDAV (Nextcloud, ownCloud, generic)

Auth flow: rclone has its own OAuth helpers. For Drive / Dropbox /
OneDrive, the user runs `rclone authorize 'drive'` on any computer with
a browser, signs in, copies the JSON token, and pastes it into the
admin panel. Other backends use API keys / passwords directly.
"""
from __future__ import annotations

import asyncio
import configparser
import logging
import os
import time
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import httpx

from . import config_store

logger = logging.getLogger("pawcorder.cloud")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
RCLONE_CONFIG_PATH = DATA_DIR / "config" / "rclone" / "rclone.conf"
RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")

FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "http://frigate:5000")
POLL_INTERVAL_SECONDS = 60
EVENT_LABELS = ("cat", "dog")

SUPPORTED_BACKENDS = ("drive", "dropbox", "onedrive", "b2", "s3", "webdav")


# ---- rclone config IO ---------------------------------------------------

@dataclass
class RcloneTestResult:
    ok: bool
    detail: str = ""


def _read_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if RCLONE_CONFIG_PATH.exists():
        cfg.read(RCLONE_CONFIG_PATH)
    return cfg


def _write_config(cfg: configparser.ConfigParser) -> None:
    """Atomic write — rclone.conf holds OAuth tokens that can't be
    regenerated without re-running `rclone authorize`. A torn write
    would silently strand the user without their cloud credentials."""
    from .utils import atomic_write_text

    buf = StringIO()
    cfg.write(buf)
    atomic_write_text(RCLONE_CONFIG_PATH, buf.getvalue())


def list_remotes() -> list[str]:
    cfg = _read_config()
    return cfg.sections()


def get_remote(name: str) -> dict[str, str]:
    cfg = _read_config()
    if name not in cfg:
        return {}
    return dict(cfg[name])


def save_remote(name: str, fields: dict[str, str]) -> None:
    """Replace (or create) a remote section in rclone.conf.

    `fields` must include `type` (one of SUPPORTED_BACKENDS). Other keys
    are backend-specific (token JSON for OAuth, access keys for B2/S3,
    URL+pass for WebDAV).
    """
    if not fields.get("type"):
        raise ValueError("rclone remote requires 'type'")
    cfg = _read_config()
    if name in cfg:
        cfg.remove_section(name)
    cfg.add_section(name)
    for k, v in fields.items():
        if v is not None:
            cfg.set(name, k, str(v))
    _write_config(cfg)


def delete_remote(name: str) -> bool:
    cfg = _read_config()
    if name in cfg:
        cfg.remove_section(name)
        _write_config(cfg)
        return True
    return False


# ---- rclone CLI wrappers ------------------------------------------------

async def _run_rclone(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    cmd = [RCLONE_BIN, "--config", str(RCLONE_CONFIG_PATH), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "rclone timed out"
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def test_remote(name: str) -> RcloneTestResult:
    """Verify the remote responds by listing its top-level directory."""
    if name not in list_remotes():
        return RcloneTestResult(ok=False, detail=f"remote {name!r} not configured")
    code, _stdout, stderr = await _run_rclone("lsd", f"{name}:", timeout=15)
    if code == 0:
        return RcloneTestResult(ok=True, detail="OK")
    return RcloneTestResult(ok=False, detail=stderr.splitlines()[-1] if stderr else f"exit {code}")


async def upload_file(name: str, local_path: Path, remote_subpath: str) -> bool:
    """Copy a single file. remote_subpath is relative to the remote's root."""
    if not local_path.exists():
        return False
    code, _stdout, stderr = await _run_rclone(
        "copyto",
        str(local_path),
        f"{name}:{remote_subpath}",
        "--no-update-modtime",
        timeout=300,
    )
    if code != 0:
        logger.warning("rclone copyto failed for %s: %s", local_path, stderr.strip()[:200])
        return False
    return True


async def cleanup_old(name: str, remote_path: str, retain_days: int) -> int:
    """Delete files older than retain_days from the remote. Returns 0 on success."""
    code, _stdout, _stderr = await _run_rclone(
        "delete",
        f"{name}:{remote_path}",
        "--min-age", f"{int(retain_days)}d",
        timeout=600,
    )
    return code


# ---- quota / size accounting -------------------------------------------

@dataclass
class CloudQuota:
    quota_supported: bool          # backend reports total/free?
    total_bytes: int = 0           # 0 when unsupported
    free_bytes: int = 0
    used_bytes: int = 0            # everything in the account, not just pawcorder
    pawcorder_bytes: int = 0       # only what we wrote
    error: str = ""


async def get_quota(name: str, remote_path: str) -> CloudQuota:
    """Best-effort: ask the backend how much space it has, and how much of
    its used space is from pawcorder's own folder.

    Object-storage backends (S3, B2, R2…) don't report a quota, so
    `quota_supported` is False; the user has to set their own size cap.
    """
    if name not in list_remotes():
        return CloudQuota(quota_supported=False, error=f"remote {name!r} not configured")

    quota = CloudQuota(quota_supported=False)

    # `rclone about` returns total/free/used in JSON for backends that report.
    code, stdout, _stderr = await _run_rclone("about", f"{name}:", "--json", timeout=20)
    if code == 0:
        try:
            import json
            data = json.loads(stdout)
            total = data.get("total")
            used = data.get("used")
            free = data.get("free")
            if total is not None or free is not None:
                quota.quota_supported = True
                quota.total_bytes = int(total or 0)
                quota.used_bytes = int(used or 0)
                quota.free_bytes = int(free or (quota.total_bytes - quota.used_bytes))
        except (ValueError, json.JSONDecodeError):
            pass

    # `rclone size --json` returns bytes used by a specific path. This works
    # on every backend, including the ones that don't support `about`.
    code, stdout, stderr = await _run_rclone("size", f"{name}:{remote_path}", "--json", timeout=60)
    if code == 0:
        try:
            import json
            data = json.loads(stdout)
            quota.pawcorder_bytes = int(data.get("bytes") or 0)
        except (ValueError, json.JSONDecodeError):
            pass
    elif "directory not found" in (stderr or "").lower():
        quota.pawcorder_bytes = 0  # nothing uploaded yet

    return quota


async def prune_oldest_until_under(name: str, remote_path: str, target_bytes: int) -> int:
    """Delete the oldest files in remote_path until total size ≤ target_bytes.

    Returns the number of files deleted. Implementation: list files sorted
    by ModTime ascending, delete one at a time, stop when target met.

    Uses `rclone lsjson --recursive` so it works across backends.
    """
    if target_bytes < 0:
        return 0

    code, stdout, _stderr = await _run_rclone(
        "lsjson", f"{name}:{remote_path}", "-R", "--no-mimetype", "--files-only", timeout=120,
    )
    if code != 0:
        return 0

    try:
        import json
        files = json.loads(stdout)
    except (ValueError, json.JSONDecodeError):
        return 0

    # Sort oldest first.
    files.sort(key=lambda f: f.get("ModTime", ""))
    total = sum(int(f.get("Size") or 0) for f in files)
    deleted = 0
    for f in files:
        if total <= target_bytes:
            break
        path = f.get("Path")
        if not path:
            continue
        size = int(f.get("Size") or 0)
        full = f"{name}:{remote_path}/{path}"
        del_code, _, _ = await _run_rclone("deletefile", full, timeout=30)
        if del_code == 0:
            total -= size
            deleted += 1
    return deleted


def estimate_max_for_free_space(free_bytes: int, fraction: float = 0.8) -> int:
    """Recommend a default size cap. Default: use 80% of currently free space."""
    if free_bytes <= 0:
        return 0
    return int(free_bytes * fraction)


# ---- Frigate event poller -> cloud uploader ----------------------------

class CloudUploader:
    """Background task that uploads Frigate event clips matching policy."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_seen: float = time.time()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="cloud-uploader")
            logger.info("cloud uploader started")

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
                logger.warning("cloud uploader tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        cfg = config_store.load_config()
        if not (cfg.cloud_enabled and cfg.cloud_remote_name in list_remotes()):
            return
        try:
            min_score = float(cfg.cloud_upload_min_score)
        except ValueError:
            min_score = 0.0

        events = await self._fetch_events()
        for event in events:
            label = event.get("label")
            top_score = float(event.get("top_score") or event.get("score") or 0.0)
            start_time = float(event.get("start_time") or 0)
            event_id = event.get("id")
            camera = event.get("camera", "unknown")
            if not event_id or start_time <= self._last_seen:
                continue
            if cfg.cloud_upload_only_pets and label not in EVENT_LABELS:
                continue
            if top_score < min_score:
                continue

            clip = await self._download_clip(event_id)
            if clip is None:
                continue
            try:
                date = time.strftime("%Y-%m-%d", time.gmtime(start_time))
                remote_subpath = f"{cfg.cloud_remote_path}/{date}/{camera}/{event_id}.mp4"
                ok = await upload_file(cfg.cloud_remote_name, clip, remote_subpath)
                if ok:
                    self._last_seen = max(self._last_seen, start_time)
                    logger.info("uploaded event %s to %s", event_id, remote_subpath)
            finally:
                try:
                    clip.unlink()
                except OSError:
                    pass

        # Enforce retention + size caps after uploads. These are best-effort:
        # if rclone errors, log and try again next tick.
        await self._enforce_retention(cfg)

    async def _enforce_retention(self, cfg: config_store.Config) -> None:
        # Day-based retention (already supported across all backends)
        try:
            days = int(cfg.cloud_retention_days)
        except ValueError:
            days = 0
        if days > 0:
            try:
                await cleanup_old(cfg.cloud_remote_name, cfg.cloud_remote_path, days)
            except Exception as exc:  # noqa: BLE001
                logger.warning("retention sweep failed: %s", exc)

        # Size cap (manual or adaptive).
        target_bytes = await self._size_cap_bytes(cfg)
        if target_bytes is None or target_bytes <= 0:
            return
        try:
            quota = await get_quota(cfg.cloud_remote_name, cfg.cloud_remote_path)
            if quota.pawcorder_bytes > target_bytes:
                deleted = await prune_oldest_until_under(
                    cfg.cloud_remote_name, cfg.cloud_remote_path, target_bytes,
                )
                logger.info("size-cap prune: %d files deleted", deleted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("size-cap enforce failed: %s", exc)

    async def _size_cap_bytes(self, cfg: config_store.Config) -> int | None:
        """Resolve the active size-cap based on mode. Returns None for no cap."""
        mode = (cfg.cloud_size_mode or "manual").strip()
        if mode == "adaptive":
            try:
                fraction = float(cfg.cloud_adaptive_fraction)
            except ValueError:
                fraction = 0.8
            try:
                quota = await get_quota(cfg.cloud_remote_name, cfg.cloud_remote_path)
            except Exception:  # noqa: BLE001
                return None
            if not quota.quota_supported or quota.free_bytes <= 0:
                return None
            # Adaptive: pawcorder may use up to fraction × (free + what we
            # already use). This way our cap floats up if the user clears
            # other files in their Drive.
            available = quota.free_bytes + quota.pawcorder_bytes
            return int(available * fraction)
        try:
            gb = float(cfg.cloud_max_size_gb)
        except ValueError:
            return None
        if gb <= 0:
            return None
        return int(gb * (1024 ** 3))

    async def _fetch_events(self) -> list[dict]:
        url = f"{FRIGATE_BASE_URL}/api/events"
        params = {"after": int(self._last_seen), "limit": 25, "has_clip": 1, "include_thumbnails": 0}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            data.sort(key=lambda e: e.get("start_time", 0))
            return data
        except (httpx.HTTPError, ValueError):
            return []

    async def _download_clip(self, event_id: str) -> Path | None:
        url = f"{FRIGATE_BASE_URL}/api/events/{event_id}/clip.mp4"
        out = Path("/tmp") / f"pawcorder-{event_id}.mp4"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return None
                    with out.open("wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)
            return out if out.exists() and out.stat().st_size > 0 else None
        except httpx.HTTPError:
            return None


uploader = CloudUploader()


# ---- backend-specific helpers ------------------------------------------

def fields_for_backend(backend: str, payload: dict[str, Any]) -> dict[str, str]:
    """Translate UI form payload into the rclone.conf fields rclone expects.

    We accept and validate only the fields each backend actually uses, so a
    user can't dump arbitrary keys into rclone.conf via the API.
    """
    if backend == "drive":
        # `token` is a JSON blob copied from `rclone authorize "drive"`.
        return {
            "type":   "drive",
            "scope":  payload.get("scope") or "drive",
            "token":  payload.get("token") or "",
        }
    if backend == "dropbox":
        return {"type": "dropbox", "token": payload.get("token") or ""}
    if backend == "onedrive":
        return {
            "type":       "onedrive",
            "drive_id":   payload.get("drive_id") or "",
            "drive_type": payload.get("drive_type") or "personal",
            "token":      payload.get("token") or "",
        }
    if backend == "b2":
        return {
            "type":    "b2",
            "account": payload.get("account") or "",
            "key":     payload.get("key") or "",
        }
    if backend == "s3":
        return {
            "type":              "s3",
            "provider":          payload.get("provider") or "Other",
            "endpoint":          payload.get("endpoint") or "",
            "access_key_id":     payload.get("access_key_id") or "",
            "secret_access_key": payload.get("secret_access_key") or "",
            "region":            payload.get("region") or "",
        }
    if backend == "webdav":
        return {
            "type":   "webdav",
            "url":    payload.get("url") or "",
            "vendor": payload.get("vendor") or "other",
            "user":   payload.get("user") or "",
            "pass":   payload.get("pass") or "",
        }
    raise ValueError(f"unsupported backend: {backend!r}")
