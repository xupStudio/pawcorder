"""Background health monitor.

Watches three things and pushes a Telegram alert when something looks
wrong (the user has already configured Telegram for pet alerts; this
just rides on the same channel):

  1. Storage usage on STORAGE_PATH > 90%
  2. Frigate container offline / unhealthy
  3. Camera offline (no recent frames in Frigate stats)

Each kind of alert is rate-limited so we don't flood the chat — once we
fire on a condition, we won't fire again until either it resolves and
re-trips, or 6 hours pass.

The monitor also exposes a snapshot via `current_status()` for the
/system page to show a live "all clear" / "warning" panel without the
user having to wait for an alert.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import config_store, docker_ops, telegram as tg

logger = logging.getLogger("pawcorder.health")

FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "http://frigate:5000")
POLL_INTERVAL_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 6 * 3600
STORAGE_WARN_FRACTION = 0.90
CAMERA_OFFLINE_AFTER_SECONDS = 300  # 5 minutes since last frame


@dataclass
class CheckResult:
    """Outcome of one health probe. `ok=True` means nothing to alert on."""
    name: str
    ok: bool
    message: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class HealthSnapshot:
    storage: CheckResult
    frigate: CheckResult
    cameras: list[CheckResult]
    overall_ok: bool
    checked_at: float

    def to_dict(self) -> dict:
        return {
            "overall_ok": self.overall_ok,
            "checked_at": self.checked_at,
            "storage": {
                "ok": self.storage.ok, "message": self.storage.message, "detail": self.storage.detail,
            },
            "frigate": {
                "ok": self.frigate.ok, "message": self.frigate.message, "detail": self.frigate.detail,
            },
            "cameras": [
                {"name": c.name, "ok": c.ok, "message": c.message, "detail": c.detail}
                for c in self.cameras
            ],
        }


# ---- individual checks --------------------------------------------------

def check_storage(path: str, warn_fraction: float = STORAGE_WARN_FRACTION) -> CheckResult:
    """Disk usage on the storage path. Path may not exist yet on a fresh
    install, in which case we silently report OK rather than yelling."""
    if not path:
        return CheckResult(name="storage", ok=True, message="not configured")
    try:
        usage = shutil.disk_usage(path)
    except (FileNotFoundError, PermissionError) as exc:
        # Don't alert: the storage path may not exist yet during initial
        # setup. The dashboard already nags about setup-incompleteness.
        return CheckResult(
            name="storage", ok=True,
            message=f"path not accessible: {exc}",
            detail={"path": path},
        )
    fraction = usage.used / usage.total if usage.total > 0 else 0
    detail = {
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_fraction": round(fraction, 3),
    }
    if fraction >= warn_fraction:
        return CheckResult(
            name="storage", ok=False,
            message=f"storage at {fraction*100:.0f}% capacity ({path})",
            detail=detail,
        )
    return CheckResult(name="storage", ok=True, message="OK", detail=detail)


def check_frigate() -> CheckResult:
    """Container running + healthcheck green."""
    try:
        status = docker_ops.get_frigate_status()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name="frigate", ok=False, message=f"docker unreachable: {exc}")
    if not status.exists:
        # No Frigate yet means setup is incomplete — not an alert-worthy
        # condition; the setup banner already covers that case.
        return CheckResult(name="frigate", ok=True, message="not yet started",
                           detail={"running": False})
    if not status.running:
        return CheckResult(
            name="frigate", ok=False, message="Frigate container is not running",
            detail={"status": status.status, "health": status.health},
        )
    # Running but unhealthy is worth shouting about.
    if status.health and status.health not in ("healthy", "starting", None):
        return CheckResult(
            name="frigate", ok=False,
            message=f"Frigate health is {status.health}",
            detail={"status": status.status, "health": status.health},
        )
    return CheckResult(
        name="frigate", ok=True, message="OK",
        detail={"status": status.status, "health": status.health},
    )


async def check_cameras() -> list[CheckResult]:
    """Per-camera liveness via Frigate's /api/<camera>/latest.jpg.

    Why this approach (and not /api/stats):

    Frigate's stats schema varies across versions — older releases don't
    expose a `last_frame` Unix-timestamp at all, and `detection_fps` /
    `camera_fps` get cached to their last non-zero value during brief
    drops, so a dead camera can keep "looking alive" for minutes. The
    `latest.jpg` endpoint, by contrast, has been stable since Frigate
    0.10 and its `Last-Modified` header reflects the actual time of the
    most recent frame served.

    A camera silent > CAMERA_OFFLINE_AFTER_SECONDS = "offline".
    """
    import email.utils  # stdlib RFC-2822 parser for Last-Modified

    out: list[CheckResult] = []

    # Fetch the camera list from /api/config (cheap; cached by Frigate).
    cameras: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{FRIGATE_BASE_URL}/api/config")
        if resp.status_code == 200:
            data = resp.json()
            cameras = list((data.get("cameras") or {}).keys())
    except (httpx.HTTPError, ValueError):
        return out

    if not cameras:
        return out

    now = time.time()
    async with httpx.AsyncClient(timeout=6.0) as client:
        for name in cameras:
            try:
                resp = await client.head(
                    f"{FRIGATE_BASE_URL}/api/{name}/latest.jpg",
                    follow_redirects=True,
                )
            except httpx.HTTPError as exc:
                out.append(CheckResult(
                    name=f"camera:{name}", ok=False,
                    message=f"camera {name} unreachable: {exc}",
                    detail={"camera": name},
                ))
                continue
            if resp.status_code != 200:
                out.append(CheckResult(
                    name=f"camera:{name}", ok=False,
                    message=f"camera {name} latest.jpg returned {resp.status_code}",
                    detail={"camera": name, "status": resp.status_code},
                ))
                continue
            last_mod = resp.headers.get("Last-Modified") or resp.headers.get("last-modified")
            age: float | None = None
            if last_mod:
                try:
                    parsed = email.utils.parsedate_to_datetime(last_mod)
                    age = now - parsed.timestamp()
                except (TypeError, ValueError):
                    age = None
            if age is not None and age > CAMERA_OFFLINE_AFTER_SECONDS:
                out.append(CheckResult(
                    name=f"camera:{name}", ok=False,
                    message=f"camera {name} no fresh frame for {int(age)}s",
                    detail={"camera": name, "offline_seconds": int(age)},
                ))
            else:
                out.append(CheckResult(
                    name=f"camera:{name}", ok=True, message="OK",
                    detail={"camera": name, "age_seconds": int(age) if age is not None else None},
                ))
    return out


# ---- snapshot composition ----------------------------------------------

async def snapshot() -> HealthSnapshot:
    cfg = config_store.load_config()
    storage = check_storage(cfg.storage_path)
    frigate = check_frigate()
    cams = await check_cameras() if frigate.ok and frigate.detail.get("status") == "running" else []
    overall = storage.ok and frigate.ok and all(c.ok for c in cams)
    return HealthSnapshot(
        storage=storage,
        frigate=frigate,
        cameras=cams,
        overall_ok=overall,
        checked_at=time.time(),
    )


# ---- background loop with rate-limited alerts --------------------------

class HealthMonitor:
    """Polls health periodically and pushes Telegram alerts.

    State lives in memory: `_last_alert[name]` is the timestamp of the
    last fire for an alert key. We don't persist this — at worst, a
    container restart costs a duplicate alert.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_alert: dict[str, float] = {}
        # Most recent snapshot, exposed via `current()`.
        self._snapshot: Optional[HealthSnapshot] = None

    def current(self) -> Optional[HealthSnapshot]:
        return self._snapshot

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="health-monitor")
            logger.info("health monitor started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("health monitor stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._snapshot = await snapshot()
                await self._maybe_alert(self._snapshot)
            except Exception as exc:  # noqa: BLE001
                logger.warning("health tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _maybe_alert(self, snap: HealthSnapshot) -> None:
        cfg = config_store.load_config()
        if not (cfg.telegram_enabled and cfg.telegram_bot_token and cfg.telegram_chat_id):
            return

        for check in (snap.storage, snap.frigate, *snap.cameras):
            if check.ok:
                # Reset cooldown on recovery so the next failure alerts again.
                self._last_alert.pop(check.name, None)
                continue
            now = time.time()
            last = self._last_alert.get(check.name, 0)
            if now - last < ALERT_COOLDOWN_SECONDS:
                continue
            try:
                await tg.send_message(
                    cfg.telegram_bot_token,
                    cfg.telegram_chat_id,
                    f"<b>pawcorder health</b>\n{check.message}",
                )
                self._last_alert[check.name] = now
            except Exception as exc:  # noqa: BLE001
                logger.warning("health alert send failed: %s", exc)


monitor = HealthMonitor()
