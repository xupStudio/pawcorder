"""LLM-generated daily diary for each pet.

Once a day (default ~22:00 local) we summarise every pet's activity
into a short, narrative paragraph and persist it to
``/data/config/diaries.ndjson``. The dashboard shows the latest entry
on /pets so the user gets a quick "how was Mochi today" without having
to scrub through events.

Two LLM backends, picked by config:

  * **Offline (Ollama)** — ``OLLAMA_BASE_URL`` is set. We POST to a
    local Ollama / OpenAI-compatible server on the user's network,
    no cloud egress, no token cost, no per-day quota. This wins over
    the two cloud backends because picking it is an explicit "I want
    privacy / I don't want to pay" decision.
  * **OSS direct** — ``OPENAI_API_KEY`` is set. We POST directly to
    api.openai.com, the user pays OpenAI for tokens. Fully self-hosted
    in the spirit of the OSS build.
  * **Pro relay** — ``PAWCORDER_PRO_LICENSE_KEY`` is set (no OpenAI
    key). We POST to the pawcorder relay endpoint and let the relay
    do the LLM call on our managed account. Pro license covers the
    quota.

If none is set the scheduler quietly no-ops — the rest of the
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

from . import config_store, recognition, reliability
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
    backend: str                       # "ollama" | "openai" | "gemini" | "anthropic" | "pro_relay"
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
    """No backend configured — caller should show a setup nudge."""


def active_backend(cfg: Optional["config_store.Config"] = None) -> Optional[str]:
    """Return which backend the next diary call would pick.

    Possible values: 'ollama', 'openai', 'gemini', 'anthropic',
    'pro_relay', or None if nothing's configured.

    Honours :attr:`Config.llm_provider_preference` — 'auto' uses the
    historical priority order; an explicit value (e.g. 'anthropic') is
    only chosen if a key is configured for it, otherwise we fall through
    to auto. Mirrors the runtime selection in :func:`generate_diary` so
    the UI can surface a "diaries via Claude Haiku" badge without
    redoing the priority rules client-side.
    """
    cfg = cfg or config_store.load_config()
    pref = (getattr(cfg, "llm_provider_preference", "auto") or "auto").lower()

    has = {
        "ollama": bool(cfg.ollama_base_url),
        "openai": bool(cfg.openai_api_key),
        "gemini": bool(getattr(cfg, "gemini_api_key", "")),
        "anthropic": bool(getattr(cfg, "anthropic_api_key", "")),
        "pro_relay": bool(cfg.pawcorder_pro_license_key),
    }
    if pref != "auto" and has.get(pref):
        return pref
    # Auto fallback order: local first (privacy/no-cost), then cloud
    # BYOK in cost-ascending order (Gemini cheapest, then OpenAI, then
    # Anthropic), then Pro relay (we eat the bill, last resort because
    # operators with their own keys usually want to use them).
    # The i18n help string SYS_LLM_PREFERENCE_HELP advertises this
    # ordering — keep them in sync if you re-tier.
    for name in ("ollama", "gemini", "openai", "anthropic", "pro_relay"):
        if has[name]:
            return name
    return None


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
        # Don't echo the vendor body — it can carry org/billing IDs.
        raise RuntimeError(f"openai http {resp.status_code}")
    data = resp.json()
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"openai: unexpected response shape ({exc})")


async def _call_ollama(base_url: str, system: str, user: str, *,
                        model: str = "qwen2.5:3b",
                        timeout: float = 60.0) -> str:
    """POST to a local Ollama / OpenAI-compatible server.

    We target Ollama's native ``/api/chat`` endpoint by default — it
    accepts the same {role, content} message shape as OpenAI but
    returns a streaming-or-single response under ``message.content``.
    A trailing ``/v1`` in ``base_url`` flips us to the OpenAI-compatible
    path (``/v1/chat/completions``) so users can point this at any
    OpenAI-shaped local proxy (LM Studio, llama.cpp's openai server,
    vLLM) without code changes.

    Local LLMs are slower than cloud, hence the higher default timeout.
    Ollama on a 4B-param model on N100 CPU is typically 8-15 s; we
    pad to 60 s to cover cold model load on the first call.
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        # OpenAI-compatible path — same body shape as cloud OpenAI.
        url = f"{base}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": 200,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"ollama http {resp.status_code}: {resp.text[:200]}")
        try:
            return str(resp.json()["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"ollama: unexpected response shape ({exc})")
    # Native Ollama API.
    url = f"{base}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": 0.7, "num_predict": 200},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"ollama http {resp.status_code}: {resp.text[:200]}")
    try:
        return str(resp.json()["message"]["content"]).strip()
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"ollama: unexpected response shape ({exc})")


async def _call_pro_relay(license_key: str, system: str, user: str, *,
                           lang: str, timeout: float = 30.0,
                           provider: Optional[str] = None) -> str:
    """Pro relay does the LLM call server-side. Body shape is small —
    license key + prompts + lang. Server returns {"diary": "..."}.

    ``provider`` (optional) lets the admin pin a specific upstream LLM
    on the relay side. None means "use the relay's default" — preserves
    legacy behaviour for callers who don't care.
    """
    payload: dict[str, object] = {
        "license_key": license_key,
        "system": system,
        "user": user,
        "lang": lang,
    }
    if provider:
        payload["provider"] = provider
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


async def _call_gemini(api_key: str, system: str, user: str, *,
                        model: str = "gemini-2.5-flash",
                        timeout: float = 30.0) -> str:
    """Direct Gemini call (BYOK). Same v1beta generateContent endpoint
    the relay uses — dual-pathing here means the OSS user (no relay)
    can still use Gemini directly.

    Mid-2026 best price/quality for short-output workloads — see
    relay/llm_provider.py for the rationale.
    """
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 200},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload,
                                  headers={"Content-Type": "application/json"})
    if resp.status_code != 200:
        # Vendor body can include billing-account hints; suppress.
        raise RuntimeError(f"gemini http {resp.status_code}")
    try:
        candidates = resp.json().get("candidates") or []
        if not candidates:
            raise RuntimeError("gemini: no candidates (safety filter?)")
        parts = candidates[0].get("content", {}).get("parts", []) or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise RuntimeError("gemini: empty response")
        return text
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"gemini: unexpected response ({exc})")


async def _call_anthropic(api_key: str, system: str, user: str, *,
                           model: str = "claude-haiku-4-5",
                           timeout: float = 30.0) -> str:
    """Direct Anthropic call (BYOK). Uses prompt caching on the system
    block (90 % off cached input) — diary system prompts are stable
    across the day so cache hit rate is high after the first call."""
    payload = {
        "model": model,
        "max_tokens": 200,
        "temperature": 0.7,
        "system": [
            {"type": "text", "text": system,
             "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages",
                                  json=payload, headers=headers)
    if resp.status_code != 200:
        # Vendor body can include billing-account hints; suppress.
        raise RuntimeError(f"anthropic http {resp.status_code}")
    try:
        data = resp.json()
        content = data.get("content") or []
        text = "".join(
            c.get("text", "") for c in content if c.get("type") == "text"
        ).strip()
        if not text:
            raise RuntimeError("anthropic: empty response")
        return text
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"anthropic: unexpected response ({exc})")


async def generate_diary(pet: Pet, *, lang: str = "en",
                          cfg: Optional[config_store.Config] = None,
                          now: Optional[float] = None,
                          sightings: Optional[list[dict]] = None,
                          openai_caller: Optional[Callable] = None,
                          relay_caller: Optional[Callable] = None,
                          ollama_caller: Optional[Callable] = None,
                          gemini_caller: Optional[Callable] = None,
                          anthropic_caller: Optional[Callable] = None) -> Diary:
    """Build summary → prompt → call backend → return Diary.

    The ``*_caller`` knobs are dependency-injection seams the tests use to
    avoid hitting the network. Production passes None and the real httpx
    clients run.

    Backend selection respects ``cfg.llm_provider_preference`` (see
    :func:`active_backend`) — operators can pin a specific vendor on the
    System settings page; "auto" preserves the historical priority order.
    """
    cfg = cfg or config_store.load_config()
    summary = build_summary(pet, now=now, sightings=sightings)
    system, user = compose_prompt(summary, lang=lang)

    # Coalesce concurrent generations for the same (pet, date). Without
    # this the scheduler tick and a manual /api/pets/diary/generate hit
    # at the same time can both call the LLM and double-bill the user.
    key = (pet.pet_id, summary.date)
    async with _inflight_lock:
        if key in _inflight_keys:
            raise RuntimeError("diary already being generated for this pet today")
        _inflight_keys.add(key)
    try:
        backend = active_backend(cfg)
        if backend is None:
            raise DiaryNotConfigured(
                "no LLM backend configured — set OLLAMA_BASE_URL, "
                "OPENAI_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY, "
                "or a Pro license to enable the diary"
            )
        if backend == "ollama":
            caller = ollama_caller or _call_ollama
            text = await caller(cfg.ollama_base_url, system, user,
                                  model=cfg.ollama_model or "qwen2.5:3b")
        elif backend == "openai":
            caller = openai_caller or _call_openai
            text = await caller(cfg.openai_api_key, system, user)
        elif backend == "gemini":
            caller = gemini_caller or _call_gemini
            text = await caller(cfg.gemini_api_key, system, user)
        elif backend == "anthropic":
            caller = anthropic_caller or _call_anthropic
            text = await caller(cfg.anthropic_api_key, system, user)
        elif backend == "pro_relay":
            caller = relay_caller or _call_pro_relay
            # If the operator picked a specific *cloud* upstream on the
            # relay side (e.g. "anthropic"), forward that hint so the
            # relay's dispatcher honours it. We only forward providers
            # the relay actually supports — "ollama" / "pro_relay" are
            # admin-side preferences with no relay equivalent and would
            # 400 there. None = let the relay pick its own default.
            pref = (cfg.llm_provider_preference or "auto").lower()
            relay_provider = pref if pref in ("openai", "gemini", "anthropic") else None
            text = await caller(cfg.pawcorder_pro_license_key, system, user,
                                  lang=lang, provider=relay_provider)
        else:
            raise DiaryNotConfigured(f"unknown backend: {backend}")

        # Record success in the reliability ledger so /reliability can
        # show "diary success rate over last 7 days". Done AFTER the
        # backend call returns so partial failures don't get marked OK.
        reliability.record(
            "ai_inference", f"diary:{backend}", "ok",
            message=f"diary generated for {pet.pet_id}",
        )
        return Diary(
            pet_id=pet.pet_id, pet_name=pet.name, date=summary.date,
            text=text, backend=backend, lang=lang,
            generated_at=now or time.time(),
        )
    except DiaryNotConfigured:
        # Not a reliability incident — user just hasn't set up a backend.
        raise
    except RuntimeError as exc:
        # Backend errored (HTTP non-200, timeout, etc). Record and
        # re-raise — caller logs + 502s as before. Use whichever
        # backend variable was bound (set inside the if/elif chain
        # above) so the row pinpoints which path failed.
        backend_label = locals().get("backend", "unknown")
        reliability.record(
            "ai_inference", f"diary:{backend_label}", "fail",
            message=str(exc)[:200],
        )
        raise
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
        if active_backend(cfg) is None:
            return  # nothing to do — no backend configured
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
