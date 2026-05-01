"""Weekly pet podcast — diary digest → script → TTS → mp3.

Once a week (Sunday evening by default) we:

  1. Pull the last 7 days of diaries from ``pet_diary.read_diaries``.
  2. Stitch them into a 60-180 second narrated script (one section
     per pet, with a friendly intro and outro).
  3. POST the script to the relay's ``/v1/tts``, get back mp3 bytes.
  4. Save under ``/data/podcasts/<YYYY-MM-DD>.mp3`` plus a sibling
     ``.json`` with metadata (script text, length, generated_at).

Reads use the relay path only (Pro feature) — OSS users can still
generate diaries via OpenAI direct, but TTS is gated by the relay so
it shows up as a Pro feature with a clear upgrade prompt.

The mp3 file is on disk so the user can download it, push to
podcasts apps, or play in the browser. Retention: keep last 8 weeks,
prune older.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import config_store, pet_diary, reliability
from .pets_store import Pet, PetStore
from .utils import PollingTask

logger = logging.getLogger("pawcorder.podcast")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
PODCAST_DIR = DATA_DIR / "podcasts"

PRO_RELAY_BASE = os.environ.get(
    "PAWCORDER_RELAY_BASE", "https://relay.pawcorder.app"
).rstrip("/")
TTS_PATH = "/v1/tts"

# Cap script length so a noisy household doesn't blow the relay
# MAX_SCRIPT_CHARS=4000. Per-pet sections are also capped below.
MAX_SCRIPT_CHARS = 3500
MAX_PER_PET_LINES = 5
RETAIN_PODCASTS = 8

# Day-of-week + hour the scheduler runs. Sundays at 21:00 local time
# = "fresh by Monday morning commute" without conflicting with the
# nightly diary scheduler at 22:00.
RUN_DAY_OF_WEEK = int(os.environ.get("PAWCORDER_PODCAST_DAY", "6"))   # 0=Mon
RUN_HOUR = int(os.environ.get("PAWCORDER_PODCAST_HOUR", "21"))


@dataclass
class Podcast:
    """One persisted episode."""
    date: str                  # local YYYY-MM-DD (Sunday)
    script: str
    audio_path: str            # absolute path to the mp3
    audio_bytes: int = 0
    pets_covered: list[str] = field(default_factory=list)
    generated_at: float = 0.0
    backend: str = "pro_relay"

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "script": self.script,
            "audio_path": self.audio_path,
            "audio_bytes": self.audio_bytes,
            "pets_covered": self.pets_covered,
            "generated_at": self.generated_at,
            "backend": self.backend,
        }


# ---- script builder ---------------------------------------------------


def build_script(*, pets: list[Pet], diaries: list[dict],
                  lang: str = "en") -> tuple[str, list[str]]:
    """Stitch a week of diaries into a narrated podcast script.

    Returns (script, pets_covered_in_script). If no diaries exist for
    a pet in the window, that pet is skipped (no script section).
    """
    is_zh = lang.startswith("zh")
    by_pet: dict[str, list[dict]] = {}
    for d in diaries:
        by_pet.setdefault(d.get("pet_id") or "", []).append(d)

    lines: list[str] = []
    if is_zh:
        lines.append("這是 pawcorder 本週寵物廣播。")
    else:
        lines.append("Welcome to your weekly pawcorder pet podcast.")

    pets_covered: list[str] = []
    for pet in pets:
        pet_diaries = by_pet.get(pet.pet_id) or []
        if not pet_diaries:
            continue
        # Newest first so the user hears the most recent day first.
        pet_diaries.sort(key=lambda r: r.get("generated_at", 0), reverse=True)
        snippets = [d.get("text", "").strip()
                    for d in pet_diaries[:MAX_PER_PET_LINES]
                    if d.get("text")]
        if not snippets:
            continue
        if is_zh:
            lines.append(f"來看看 {pet.name} 這週過得怎麼樣。")
        else:
            lines.append(f"Here's how {pet.name} has been this week.")
        lines.extend(snippets)
        pets_covered.append(pet.pet_id)

    if not pets_covered:
        # No data at all — emit a tiny placeholder so the user gets
        # *something* and the scheduler doesn't spam the relay quota.
        if is_zh:
            lines.append("這週沒有足夠的資料生成廣播，週末再見囉。")
        else:
            lines.append("Not enough data this week — see you next Sunday.")

    if is_zh:
        lines.append("祝你和毛孩有個美好的一週。")
    else:
        lines.append("Have a great week with your pets.")

    script = "\n\n".join(lines)
    if len(script) > MAX_SCRIPT_CHARS:
        # Trim mid-section rather than breaking the outro — keeps the
        # podcast feeling complete even when a household has many pets.
        script = script[:MAX_SCRIPT_CHARS - 80].rstrip()
        if is_zh:
            script += "\n\n（本週內容超過時長，有些片段已省略。）祝你和毛孩有個美好的一週。"
        else:
            script += "\n\n(Some sections trimmed for length.) Have a great week."
    return script, pets_covered


# ---- TTS call ----------------------------------------------------------


async def synthesize(script: str, license_key: str, *,
                      voice: str = "alloy",
                      provider: Optional[str] = None,
                      base: Optional[str] = None,
                      tts_caller: Optional[Callable] = None,
                      timeout: float = 120.0) -> bytes:
    """POST script to the relay TTS endpoint, return mp3 bytes.

    ``provider`` (optional, e.g. 'cartesia', 'elevenlabs', 'openai',
    'xtts') overrides the relay's default TTS engine. ``voice`` is
    a friendly alias the relay translates to vendor-specific voice IDs
    — pass empty to let the relay pick a sensible default.
    """
    if tts_caller is not None:
        return await tts_caller(license_key, script, voice)
    base = (base or PRO_RELAY_BASE).rstrip("/")
    payload: dict[str, object] = {
        "license_key": license_key,
        "script": script,
        "voice": voice,
    }
    if provider:
        payload["provider"] = provider
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(base + TTS_PATH, json=payload)
    if resp.status_code == 401:
        raise RuntimeError("podcast: license invalid")
    if resp.status_code == 429:
        raise RuntimeError("podcast: quota exceeded")
    if resp.status_code != 200:
        raise RuntimeError(
            f"podcast tts http {resp.status_code}: {resp.text[:200]}"
        )
    return resp.content


# ---- persistence ------------------------------------------------------


def save_podcast(podcast: Podcast, audio: bytes) -> None:
    """Atomically write the mp3 + sidecar metadata. Prunes old episodes.

    The mp3 can be megabytes — a crash mid-`write_bytes` would leave a
    truncated file that the prune step would then count as a valid
    episode. Stage to ``.partial`` then ``os.replace`` so readers
    (the /pets page lists episodes) only ever see complete files.
    """
    import os as _os
    PODCAST_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = PODCAST_DIR / f"{podcast.date}.mp3"
    meta_path = PODCAST_DIR / f"{podcast.date}.json"
    tmp_audio = audio_path.with_suffix(audio_path.suffix + ".partial")
    tmp_meta = meta_path.with_suffix(meta_path.suffix + ".partial")
    tmp_audio.write_bytes(audio)
    podcast.audio_bytes = len(audio)
    podcast.audio_path = str(audio_path)
    tmp_meta.write_text(
        json.dumps(podcast.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _os.replace(tmp_audio, audio_path)
    _os.replace(tmp_meta, meta_path)
    _prune(retain=RETAIN_PODCASTS)


def _prune(*, retain: int) -> None:
    """Keep only the N newest mp3 files (by date in filename)."""
    if not PODCAST_DIR.exists():
        return
    pairs: list[tuple[str, Path]] = []
    for p in PODCAST_DIR.iterdir():
        if p.suffix == ".mp3":
            pairs.append((p.stem, p))
    pairs.sort(key=lambda kv: kv[0], reverse=True)
    for _, mp3 in pairs[retain:]:
        try:
            mp3.unlink()
        except OSError:
            pass
        sidecar = mp3.with_suffix(".json")
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass


def list_podcasts() -> list[dict]:
    """Newest-first list of episodes for the /pets page."""
    if not PODCAST_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(PODCAST_DIR.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


# ---- scheduler --------------------------------------------------------


class PodcastScheduler(PollingTask):
    """Once a week, on RUN_DAY_OF_WEEK at RUN_HOUR local time, render
    the podcast. Hourly poll because we don't want to miss the slot
    if the host slept through the exact hour."""

    name = "weekly-podcast-scheduler"
    interval_seconds = 3600.0

    async def _tick(self) -> None:
        cfg = config_store.load_config()
        if not cfg.pawcorder_pro_license_key:
            return  # Pro-only feature
        now = time.time()
        local = time.localtime(now)
        if local.tm_wday != RUN_DAY_OF_WEEK or local.tm_hour < RUN_HOUR:
            return
        today = time.strftime("%Y-%m-%d", local)
        # Idempotent — if we already produced today's episode, skip.
        if (PODCAST_DIR / f"{today}.mp3").exists():
            return

        pets = PetStore().load()
        if not pets:
            return
        # 7 days of diaries × pet count → reasonable cap of 100 entries.
        diaries = pet_diary.read_diaries(limit=100)
        # Filter to last 7 days. Diary records use ISO date strings.
        cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 7 * 86400))
        diaries = [d for d in diaries if (d.get("date") or "") >= cutoff]
        lang = os.environ.get("PAWCORDER_DIARY_LANG", "en")
        script, covered = build_script(pets=pets, diaries=diaries, lang=lang)
        # Honour operator's TTS provider + voice picks from System
        # settings. None means the relay decides ('auto' default).
        tts_pref = (getattr(cfg, "tts_provider_preference", "auto") or "auto").lower()
        provider = None if tts_pref in ("auto", "") else tts_pref
        voice = cfg.tts_voice or "alloy"
        try:
            audio = await synthesize(
                script, cfg.pawcorder_pro_license_key,
                voice=voice, provider=provider,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("podcast TTS failed: %s", exc)
            reliability.record(
                "ai_inference", "podcast:pro_relay", "fail",
                message=str(exc)[:200],
            )
            return
        reliability.record("ai_inference", "podcast:pro_relay", "ok")
        podcast = Podcast(
            date=today, script=script,
            audio_path="",   # filled in by save_podcast
            pets_covered=covered,
            generated_at=now,
        )
        save_podcast(podcast, audio)
        logger.info("podcast %s saved (%d pets, %d bytes)",
                     today, len(covered), len(audio))


scheduler = PodcastScheduler()
