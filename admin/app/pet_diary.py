"""LLM-generated daily diary for each pet.

Once a day (default ~22:00 local) we summarise every pet's activity
into a short, narrative paragraph and persist it to
``/data/config/diaries.ndjson``. The dashboard shows the latest entry
on /pets so the user gets a quick "how was Mochi today" without having
to scrub through events.

Two LLM backends, picked by config:

  * **OSS direct** — ``OPENAI_API_KEY`` is set. We POST directly to
    api.openai.com, the user pays OpenAI for tokens. Fully self-hosted
    in the spirit of the OSS build.
  * **Pro relay** — ``PAWCORDER_PRO_LICENSE_KEY`` is set (no OpenAI
    key). We POST to the pawcorder relay endpoint and let the relay
    do the LLM call on our managed account. Pro license covers the
    quota.

If neither is set the scheduler quietly no-ops — the rest of the
admin keeps working. We never crash the user's system because the
optional AI feature isn't configured.

The prompt we send is purely structured stats (counts, hours, camera
names) — no event IDs, no snapshots, no PII. The relay only sees the
prompt + license key, never the user's raw data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import config_store, recognition
from .pets_store import Pet, PetStore
from .utils import PollingTask, atomic_write_text

logger = logging.getLogger("pawcorder.pet_diary")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
DIARIES_LOG = DATA_DIR / "config" / "diaries.ndjson"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("PAWCORDER_DIARY_MODEL", "gpt-4o-mini")

# Pro relay endpoint — the env override is so internal tests can point
# at a fixture server without touching the prod URL.
PRO_RELAY_BASE = os.environ.get(
    "PAWCORDER_RELAY_BASE", "https://relay.pawcorder.app"
).rstrip("/")
PRO_RELAY_PATH = "/v1/diary"

# Soft cap on stored entries — 365 days * (≤10 pets) = 3650 lines, < 1 MB.
MAX_DIARY_LINES = 5_000

# Generate at most one diary per pet per day, even if the scheduler
# wakes up twice. Same model recognition_backfill uses for "in flight".
_log_lock = threading.Lock()

# Async lock so the daily scheduler and the /api/pets/diary/generate
# route can't both call OpenAI (= double-bill) for the same (pet, date).
# Threaded lock can't span asyncio scopes; this one wraps the
# generate-then-append cycle.
_inflight_lock = asyncio.Lock()
_inflight_keys: set[tuple[str, str]] = set()


@dataclass
class DiarySummary:
    """Structured stats fed into the LLM prompt — no PII, no raw events."""
    pet_id: str
    pet_name: str
    species: str
    date: str                           # local YYYY-MM-DD
    sightings: int = 0
    cameras: list[str] = field(default_factory=list)
    peak_hours: list[int] = field(default_factory=list)   # local hours, 0..23
    quiet_hours: list[int] = field(default_factory=list)
    last_seen_hour: Optional[int] = None
    high_confidence_count: int = 0


@dataclass
class Diary:
    """One persisted diary entry."""
    pet_id: str
    pet_name: str
    date: str
    text: str
    backend: str                       # "openai" | "pro_relay"
    lang: str = "en"
    generated_at: float = 0.0          # unix seconds

    def to_dict(self) -> dict:
        return {
            "pet_id": self.pet_id,
            "pet_name": self.pet_name,
            "date": self.date,
            "text": self.text,
            "backend": self.backend,
            "lang": self.lang,
            "generated_at": self.generated_at,
        }


class DiaryNotConfigured(RuntimeError):
    """No OpenAI key and no Pro license — caller should show a setup nudge."""


# ---- summary builder ---------------------------------------------------

def build_summary(pet: Pet, *, now: Optional[float] = None,
                  sightings: Optional[list[dict]] = None) -> DiarySummary:
    """Bucket today's sightings into the stats the prompt consumes.

    We accept an explicit `sightings` list so unit tests don't need to
    monkey-patch the recognition module — pass a fake list and the
    function is pure.
    """
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    summary = DiarySummary(
        pet_id=pet.pet_id, pet_name=pet.name, species=pet.species, date=today
    )
    if sightings is None:
        sightings = [
            r for r in recognition.read_sightings(limit=10_000, since=now - 86400)
            if r.get("pet_id") == pet.pet_id
        ]
    if not sightings:
        return summary

    cams: dict[str, int] = {}
    by_hour = [0] * 24
    confident = 0
    last_ts = 0.0
    for r in sightings:
        ts = float(r.get("start_time") or 0)
        if ts <= 0:
            continue
        cams[str(r.get("camera") or "")] = cams.get(str(r.get("camera") or ""), 0) + 1
        h = time.localtime(ts).tm_hour
        by_hour[h] += 1
        if r.get("confidence") == "high":
            confident += 1
        if ts > last_ts:
            last_ts = ts

    summary.sightings = sum(by_hour)
    summary.cameras = [c for c, _ in sorted(cams.items(), key=lambda kv: -kv[1]) if c]
    summary.high_confidence_count = confident
    if last_ts:
        summary.last_seen_hour = time.localtime(last_ts).tm_hour
    # Peak = top-3 hours by count (ignoring zero-count). Quiet = top-3
    # consecutive zero-count blocks. Both kept short — the LLM doesn't
    # need a histogram, just a hint.
    nonzero = [(h, by_hour[h]) for h in range(24) if by_hour[h] > 0]
    nonzero.sort(key=lambda x: (-x[1], x[0]))
    summary.peak_hours = [h for h, _ in nonzero[:3]]
    summary.quiet_hours = [h for h in range(24) if by_hour[h] == 0][:6]
    return summary


# ---- prompt builder ----------------------------------------------------

_SYSTEM_PROMPT_EN = (
    "You write a short, warm daily diary for a pet, in the first person "
    "(the pet narrating). Two to four sentences. No invented events — "
    "use only the stats provided. Keep it light and observational."
)
_SYSTEM_PROMPT_ZH = (
    "你以寵物的第一人稱寫一則溫暖簡短的當日日記，2 到 4 句。"
    "只能使用提供的數據，不要編造事件。語氣輕鬆生活感。"
)


def compose_prompt(summary: DiarySummary, lang: str = "en") -> tuple[str, str]:
    """Returns (system_prompt, user_prompt). Caller assembles the chat."""
    sys_prompt = _SYSTEM_PROMPT_ZH if lang.startswith("zh") else _SYSTEM_PROMPT_EN
    cams = ", ".join(summary.cameras[:4]) or "no cameras"
    peaks = ", ".join(f"{h:02d}:00" for h in summary.peak_hours) or "—"
    user_prompt = (
        f"Pet: {summary.pet_name} ({summary.species})\n"
        f"Date: {summary.date}\n"
        f"Sightings: {summary.sightings}\n"
        f"Active cameras: {cams}\n"
        f"Peak hours: {peaks}\n"
        f"High-confidence sightings: {summary.high_confidence_count}\n"
    )
    if summary.last_seen_hour is not None:
        user_prompt += f"Last seen at hour: {summary.last_seen_hour:02d}\n"
    return sys_prompt, user_prompt


# ---- LLM calls ---------------------------------------------------------

async def _call_openai(api_key: str, system: str, user: str, *,
                        model: str = OPENAI_MODEL,
                        timeout: float = 30.0) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "max_tokens": 200,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(OPENAI_URL, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"openai http {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"openai: unexpected response shape ({exc})")


async def _call_pro_relay(license_key: str, system: str, user: str, *,
                           lang: str, timeout: float = 30.0) -> str:
    """Pro relay does the LLM call server-side. Body shape is small —
    license key + prompts + lang. Server returns {"diary": "..."}."""
    payload = {
        "license_key": license_key,
        "system": system,
        "user": user,
        "lang": lang,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(PRO_RELAY_BASE + PRO_RELAY_PATH, json=payload)
    if resp.status_code == 401:
        raise RuntimeError("pro relay: license invalid")
    if resp.status_code == 429:
        raise RuntimeError("pro relay: quota exceeded")
    if resp.status_code != 200:
        raise RuntimeError(f"pro relay http {resp.status_code}: {resp.text[:200]}")
    try:
        return str(resp.json()["diary"]).strip()
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"pro relay: unexpected response ({exc})")


async def generate_diary(pet: Pet, *, lang: str = "en",
                          cfg: Optional[config_store.Config] = None,
                          now: Optional[float] = None,
                          sightings: Optional[list[dict]] = None,
                          openai_caller: Optional[Callable] = None,
                          relay_caller: Optional[Callable] = None) -> Diary:
    """Build summary → prompt → call backend → return Diary.

    The two `*_caller` knobs are dependency-injection seams the tests
    use to avoid hitting the network. Production passes None and the
    real httpx clients run.
    """
    cfg = cfg or config_store.load_config()
    summary = build_summary(pet, now=now, sightings=sightings)
    system, user = compose_prompt(summary, lang=lang)

    # Coalesce concurrent generations for the same (pet, date). Without
    # this the scheduler tick and a manual /api/pets/diary/generate hit
    # at the same time can both call OpenAI and double-bill the user.
    key = (pet.pet_id, summary.date)
    async with _inflight_lock:
        if key in _inflight_keys:
            raise RuntimeError("diary already being generated for this pet today")
        _inflight_keys.add(key)
    try:
        # Backend selection: explicit OpenAI key wins (user opted into
        # paying directly). License-only installs fall back to relay.
        if cfg.openai_api_key:
            backend = "openai"
            caller = openai_caller or _call_openai
            text = await caller(cfg.openai_api_key, system, user)
        elif cfg.pawcorder_pro_license_key:
            backend = "pro_relay"
            caller = relay_caller or _call_pro_relay
            text = await caller(cfg.pawcorder_pro_license_key, system, user, lang=lang)
        else:
            raise DiaryNotConfigured(
                "no OpenAI key and no Pro license — set one to enable the diary"
            )

        return Diary(
            pet_id=pet.pet_id, pet_name=pet.name, date=summary.date,
            text=text, backend=backend, lang=lang,
            generated_at=now or time.time(),
        )
    finally:
        _inflight_keys.discard(key)


# ---- persistence -------------------------------------------------------

def append_diary(d: Diary) -> None:
    """Append to NDJSON. We allow at most one entry per (pet_id, date) —
    a re-run on the same day overwrites the previous text in-place."""
    DIARIES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock:
        existing = read_diaries(limit=MAX_DIARY_LINES)
        filtered = [
            r for r in existing
            if not (r.get("pet_id") == d.pet_id and r.get("date") == d.date)
        ]
        filtered.append(d.to_dict())
        # Newest last on disk so a tail -f looks chronological.
        filtered.sort(key=lambda r: (r.get("date", ""), r.get("generated_at", 0)))
        if len(filtered) > MAX_DIARY_LINES:
            filtered = filtered[-MAX_DIARY_LINES:]
        atomic_write_text(
            DIARIES_LOG,
            "\n".join(json.dumps(r, ensure_ascii=False) for r in filtered) + "\n",
        )


def read_diaries(*, pet_id: Optional[str] = None, limit: int = 30) -> list[dict]:
    from .utils import read_ndjson
    return read_ndjson(
        DIARIES_LOG,
        filter_fn=(lambda r: r.get("pet_id") == pet_id) if pet_id else None,
        sort_key=lambda r: (r.get("date", ""), r.get("generated_at", 0)),
        reverse=True,
        limit=limit,
    )


# ---- scheduler ---------------------------------------------------------

class DiaryScheduler(PollingTask):
    """Generate diaries once per day, around DAILY_HOUR local time.

    The check runs hourly so we don't miss the window if the host was
    asleep at the exact hour. If a diary for today already exists for
    a pet, we skip — no double-writes.
    """

    DAILY_HOUR = int(os.environ.get("PAWCORDER_DIARY_HOUR", "22"))
    name = "pet-diary-scheduler"
    interval_seconds = 3600.0

    async def _tick(self) -> None:
        now = time.time()
        if time.localtime(now).tm_hour < self.DAILY_HOUR:
            return
        cfg = config_store.load_config()
        if not (cfg.openai_api_key or cfg.pawcorder_pro_license_key):
            return  # nothing to do — neither backend configured
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        already = {
            r.get("pet_id") for r in read_diaries(limit=200)
            if r.get("date") == today
        }
        for pet in PetStore().load():
            if pet.pet_id in already:
                continue
            try:
                d = await generate_diary(pet, lang=os.environ.get("PAWCORDER_DIARY_LANG", "en"),
                                          cfg=cfg, now=now)
                append_diary(d)
            except DiaryNotConfigured:
                return  # config changed under us — bail cleanly
            except Exception as exc:  # noqa: BLE001
                logger.warning("diary generate failed for %s: %s", pet.pet_id, exc)


scheduler = DiaryScheduler()
