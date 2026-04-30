"""Lightweight over-the-air update checker.

How updates work in pawcorder:

  - Day-to-day, Watchtower (a separate container in docker-compose.yml)
    polls Docker Hub / GHCR every hour and pulls fresh image tags. The
    user sets the cadence in CONFIG / leaves it alone.
  - This module gives the admin UI a "current version" badge plus a
    "check for updates" button that hits the GitHub Releases API to see
    if a newer release of pawcorder itself is out (separate from the
    Frigate base image).

We deliberately don't auto-restart on update — the admin panel is meant
to be the user's stable surface; only the Watchtower-pulled containers
get hot-swapped underneath us.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from .utils import PollingTask, atomic_write_text

logger = logging.getLogger("pawcorder.updater")

# Persisted state: the last version the user dismissed via the
# dashboard banner ("skip this version"). Lets us hide the nag for one
# specific release without disabling future-update detection. Lives in
# DATA_DIR/config so it travels with backups.
DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))


def _skip_path() -> Path:
    return DATA_DIR / "config" / "update_skipped.json"


def load_skipped_version() -> str:
    """Return the tag the user previously chose to skip, or "" if none."""
    p = _skip_path()
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("skipped_version") or "")


def save_skipped_version(tag: str) -> None:
    """Persist the user's "ignore this release" choice. Empty clears it."""
    atomic_write_text(
        _skip_path(),
        json.dumps({"skipped_version": tag, "saved_at": int(time.time())},
                    ensure_ascii=False, indent=2),
    )

# Embedded at build time. Falls back to "dev" so local devs see something.
APP_VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
DEFAULT_VERSION = "dev"

GITHUB_LATEST = "https://api.github.com/repos/xupStudio/pawcorder/releases/latest"
GITHUB_REPO_URL_OVERRIDE = os.environ.get("PAWCORDER_RELEASES_URL")  # for self-forks

# In-memory cache: don't hammer GitHub on every UI render.
_CACHE_TTL_SECONDS = 60 * 30  # 30 min
_cache: dict[str, object] = {"checked_at": 0.0, "result": None}


@dataclass
class UpdateCheck:
    current_version: str
    latest_version: Optional[str]
    update_available: bool
    release_url: str = ""
    release_notes: str = ""
    error: str = ""
    checked_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "release_url": self.release_url,
            "release_notes": self.release_notes,
            "error": self.error,
            "checked_at": self.checked_at,
        }


def current_version() -> str:
    """Read VERSION file written at image-build time. Returns 'dev' on miss."""
    try:
        return APP_VERSION_FILE.read_text(encoding="utf-8").strip() or DEFAULT_VERSION
    except (FileNotFoundError, OSError):
        return DEFAULT_VERSION


def _normalize(v: str) -> tuple[int, ...]:
    """Turn 'v1.2.3', '1.2.3-rc1' etc. into a tuple for comparison.

    Anything non-numeric in a segment becomes 0. This is intentionally
    forgiving — we'd rather underflag an update than crash the admin
    page on a release tagged 'banana'.
    """
    s = v.strip().lstrip("v").split("-", 1)[0]
    parts = re.split(r"[.+]", s)
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def is_newer(latest: str, current: str) -> bool:
    if not latest or current == DEFAULT_VERSION:
        # Devs running unbuilt code shouldn't see an "update available"
        # nag pointing at the latest official release.
        return False
    return _normalize(latest) > _normalize(current)


async def fetch_latest_release(timeout: float = 8.0) -> dict:
    """Hit the GitHub Releases API. Raises on network or parse failure."""
    url = GITHUB_REPO_URL_OVERRIDE or GITHUB_LATEST
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
    if resp.status_code != 200:
        raise RuntimeError(f"GitHub returned {resp.status_code}")
    return resp.json()


async def check_for_updates(*, force: bool = False) -> UpdateCheck:
    """Cached update check. Returns last result if fresh enough unless `force`."""
    now = time.time()
    if not force:
        ts = float(_cache.get("checked_at") or 0)
        cached = _cache.get("result")
        if cached and (now - ts) < _CACHE_TTL_SECONDS:
            return cached  # type: ignore[return-value]

    cur = current_version()
    try:
        data = await fetch_latest_release()
        latest_tag = (data.get("tag_name") or data.get("name") or "").strip()
        notes = data.get("body") or ""
        url = data.get("html_url") or ""
        result = UpdateCheck(
            current_version=cur,
            latest_version=latest_tag or None,
            update_available=is_newer(latest_tag, cur),
            release_url=url,
            release_notes=notes[:1000],  # cap so admin page stays small
            checked_at=now,
        )
    except Exception as exc:  # noqa: BLE001
        # Soft-fail: a network glitch shouldn't break the UI.
        result = UpdateCheck(
            current_version=cur, latest_version=None,
            update_available=False, error=str(exc), checked_at=now,
        )
    _cache["checked_at"] = now
    _cache["result"] = result
    return result


# ---- background poll -------------------------------------------------

class UpdateChecker(PollingTask):
    """Refresh the update cache once a day so the dashboard banner has
    fresh data without depending on the user opening /system. The
    on-demand check still works (force=True) — this is a backstop."""

    name = "update-checker"
    # 24h between automatic checks. Releases are infrequent; hammering
    # the GitHub API more often serves no purpose and risks rate-limit.
    interval_seconds = 24 * 3600.0

    async def _tick(self) -> None:
        await check_for_updates(force=True)


checker = UpdateChecker()


# ---- "apply update" action ------------------------------------------
#
# We trigger `docker compose pull && docker compose up -d` from inside
# the admin container by going through docker_ops, which already has the
# docker socket mounted (see docker-compose.yml). The host-orchestrated
# alternative (ssh + pull + up) is the user's responsibility on bare
# metal — that's the install style we recommend explicitly NOT here.
#
# Side effects: this call restarts the admin container itself, so the
# HTTP response will land in a window where the request is in-flight
# but the response can never be sent. We document that for the UI:
# show a "applying — page will reload in ~30s" toast and re-poll
# /api/status once the new instance comes back.

@dataclass
class ApplyOutcome:
    ok: bool
    message: str
    detail: str = ""


async def apply_update_compose() -> ApplyOutcome:
    """Run `docker compose pull && docker compose up -d` in the host
    project dir. Best-effort: if the user's host is set up differently
    (no docker, custom compose file path), surface a clear error
    instead of trying to "fix" it."""
    from . import docker_ops
    if not hasattr(docker_ops, "compose_pull_and_up"):
        return ApplyOutcome(
            ok=False, message="not_supported",
            detail="docker_ops.compose_pull_and_up missing — host install style?",
        )
    try:
        result = await docker_ops.compose_pull_and_up()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return ApplyOutcome(ok=False, message="apply_failed", detail=str(exc))
    if not result.get("ok"):
        return ApplyOutcome(ok=False, message="apply_failed",
                             detail=str(result.get("stderr") or ""))
    return ApplyOutcome(ok=True, message="restarting",
                         detail="docker compose pull && up succeeded; "
                                "admin will reconnect in ~30s")
