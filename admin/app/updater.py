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

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("pawcorder.updater")

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
