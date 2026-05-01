"""Natural-language Q&A over the pet sightings timeline.

User types "Did Mochi jump on the table today?" / "When did the cats
last fight?" / "麻糬今天有上桌嗎？" — we pull the relevant slice of
the sightings log, hand it to the LLM with a tight prompt, and return
a short text answer plus the event IDs the model based it on.

Why we don't use OpenAI tool-calling: Ollama small models, the Pro
relay's gpt-4o-mini, and direct OpenAI all support the same
"system + user + JSON-shaped context" pattern. Tool-calling
varies across backends and breaks Ollama's smaller models entirely.
A pre-filter + grounded summary works everywhere.

Pipeline:

  1. Parse the question for cheap hints (time scope, pet name match,
     camera name match) — regex only, no LLM round-trip.
  2. Pull a focused slice of sightings (default last 24 h, expanded
     to 7 days when the question contains "this week" / "本週").
  3. Compact each row to ~5 fields. Cap at 200 rows so we don't
     blow the model's context.
  4. Send {question, sightings_table, pet_list} to the active
     diary backend. Ask for a short answer + cited event_ids.
  5. Parse out the citations so the UI can link them back to the
     timeline.

The whole thing runs through ``pet_diary.generate_diary``'s backend
selection so the user's Ollama / OpenAI / Pro relay choice all
"just work".
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config_store, pet_diary, recognition, reliability
from .pets_store import Pet, PetStore

logger = logging.getLogger("pawcorder.pet_query")

# Hard caps so an adversarial question can't blow up the context.
MAX_QUESTION_CHARS = 400
MAX_SIGHTINGS_IN_CONTEXT = 200
DEFAULT_WINDOW_HOURS = 24
EXTENDED_WINDOW_HOURS = 7 * 24


# Coalesce identical concurrent questions — without this a user
# mashing the "Ask" button or two browser tabs hitting the same
# endpoint both round-trip to the LLM (= 2× quota burn). Keyed by
# sha256 of the question so different phrasings don't collide.
# Limited time-of-life isn't needed — the lock is held only for the
# duration of one in-flight call; once it returns the entry is
# removed and the next identical question gets a fresh call.
_query_inflight_lock = asyncio.Lock()
_query_inflight: set[str] = set()


def _question_key(question: str) -> str:
    return hashlib.sha256(question.strip().lower().encode("utf-8")).hexdigest()

# `\b` doesn't fire between CJK chars (no ASCII word boundary), so the
# Chinese alternatives are matched without it. Two passes — one ASCII
# (with word-boundary), one CJK (substring).
_TIME_HINT_RE_WEEK_ASCII = re.compile(r"\b(this week|past week|last week)\b",
                                        re.IGNORECASE)
_TIME_HINT_WEEK_CJK = ("本週", "過去一週", "本周", "上週", "上周")


@dataclass
class QueryAnswer:
    """Returned to the route, serialised to the UI."""
    question: str
    answer: str
    event_ids: list[str] = field(default_factory=list)
    backend: str = ""           # "openai" / "pro_relay" / "ollama"
    samples_considered: int = 0
    window_hours: float = 0.0

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "event_ids": self.event_ids,
            "backend": self.backend,
            "samples_considered": self.samples_considered,
            "window_hours": self.window_hours,
        }


# ---- pre-filter --------------------------------------------------------


def _detect_window_hours(question: str) -> float:
    """Cheap heuristic: scan the question for time-scope hints.

    Default 24h; "this week" → 7×24h. Tighter scopes ("today" /
    "今天") just confirm 24h. Anything else also gets 24h —
    longer windows hurt context-quality more than they help.
    """
    if _TIME_HINT_RE_WEEK_ASCII.search(question):
        return EXTENDED_WINDOW_HOURS
    if any(token in question for token in _TIME_HINT_WEEK_CJK):
        return EXTENDED_WINDOW_HOURS
    return DEFAULT_WINDOW_HOURS


def filter_sightings(question: str, *, now: Optional[float] = None,
                      sightings: Optional[list[dict]] = None,
                      pets: Optional[list[Pet]] = None
                      ) -> tuple[list[dict], float, list[Pet]]:
    """Pre-filter the sightings log down to a model-sized slice.

    Returns (filtered_rows, window_hours, mentioned_pets). The
    `mentioned_pets` list lets the prompt nudge the model toward the
    pets the user named, without filtering them out (the model still
    benefits from cross-pet context).
    """
    now = now or time.time()
    window_hours = _detect_window_hours(question)
    if sightings is None:
        sightings = recognition.read_sightings(
            limit=10_000, since=now - window_hours * 3600,
        )
    if pets is None:
        pets = PetStore().load()

    # Pet name match — only used to surface "did the user mean Mochi?"
    # context. We don't drop other pets' rows because the question may
    # actually need them ("did the cats fight?" needs both pets).
    q_lower = question.lower()
    mentioned: list[Pet] = []
    for p in pets:
        if p.name and p.name.lower() in q_lower:
            mentioned.append(p)
        elif p.pet_id and p.pet_id.lower() in q_lower:
            mentioned.append(p)

    # Cap on rows to keep context small. Newest first so the most
    # relevant rows survive the trim.
    rows = sorted(sightings, key=lambda r: r.get("start_time", 0), reverse=True)
    rows = rows[:MAX_SIGHTINGS_IN_CONTEXT]
    return rows, window_hours, mentioned


# ---- prompt builder ---------------------------------------------------


def _format_row(row: dict) -> str:
    """Compact one-line summary for the model. Stable shape so the
    LLM can pattern-match on event ids in its citations."""
    ts = float(row.get("start_time") or 0)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
    pet = row.get("pet_name") or row.get("pet_id") or "unknown"
    cam = row.get("camera") or "?"
    conf = row.get("confidence") or "?"
    eid = row.get("event_id") or "?"
    return f"[{eid}] {when} pet={pet} camera={cam} confidence={conf}"


def compose_prompt(question: str, rows: list[dict],
                    *, pets: list[Pet], window_hours: float,
                    mentioned: list[Pet], lang: str = "en"
                    ) -> tuple[str, str]:
    """Returns (system, user). Caller routes through diary backends."""
    is_zh = lang.startswith("zh")
    if is_zh:
        sys_prompt = (
            "你是寵物攝影機資料的查詢助手。"
            "請根據下方提供的事件清單回答使用者的問題。"
            "若資料不足以回答，誠實說「資料中沒有」。"
            "回答需簡短（2-3 句），並在 'EVENTS:' 行列出引用的 event_id（以逗號分隔，"
            "至多 5 個）。不要編造未列出的事件。"
        )
    else:
        sys_prompt = (
            "You answer questions about pet camera events using ONLY the "
            "supplied table. Be brief (2-3 sentences). If the data is "
            "insufficient, say so plainly. End your reply with a line "
            "'EVENTS: <comma-separated event_ids you cited>' (max 5). "
            "Never invent events that aren't in the table."
        )

    table = "\n".join(_format_row(r) for r in rows) or "(no events in window)"
    pet_lines = ", ".join(f"{p.name} ({p.species})" for p in pets)
    mentioned_line = (
        f"User likely means: {', '.join(p.name for p in mentioned)}\n"
        if mentioned else ""
    )
    user_prompt = (
        f"Question: {question}\n"
        f"Window: last {int(window_hours)} hours\n"
        f"Known pets: {pet_lines or 'none configured'}\n"
        f"{mentioned_line}"
        f"Events ({len(rows)} rows, newest first):\n"
        f"{table}\n"
    )
    return sys_prompt, user_prompt


# ---- citation extraction ---------------------------------------------


_EVENTS_LINE_RE = re.compile(r"EVENTS:\s*([^\n]+)", re.IGNORECASE)


def extract_event_ids(answer_text: str, valid_ids: set[str]) -> list[str]:
    """Pull the EVENTS: line out of the model output. We then
    intersect with the valid set so a model hallucinating an event_id
    can never link to a nonexistent timeline entry.

    Returns up to 5 ids in the order the model emitted them."""
    m = _EVENTS_LINE_RE.search(answer_text)
    if not m:
        return []
    raw = m.group(1)
    candidates = [c.strip() for c in raw.split(",") if c.strip()]
    out: list[str] = []
    for c in candidates:
        if c in valid_ids and c not in out:
            out.append(c)
        if len(out) >= 5:
            break
    return out


def strip_events_line(answer_text: str) -> str:
    """Remove the EVENTS: trailing line so the user-visible answer is
    just the prose. Keeps the model's structured citation off the UI."""
    return _EVENTS_LINE_RE.sub("", answer_text).strip()


# ---- public entry point -----------------------------------------------


async def answer_question(question: str, *,
                          cfg: Optional[config_store.Config] = None,
                          now: Optional[float] = None,
                          sightings: Optional[list[dict]] = None,
                          pets: Optional[list[Pet]] = None,
                          openai_caller: Optional[Callable] = None,
                          relay_caller: Optional[Callable] = None,
                          ollama_caller: Optional[Callable] = None,
                          lang: str = "en") -> QueryAnswer:
    """Pre-filter → prompt → call backend → parse → return.

    Re-uses the diary backends for LLM access, so Ollama / OpenAI /
    Pro-relay all work transparently. Records every call into the
    reliability ledger as ``ai_inference / query:{backend}``.
    """
    cfg = cfg or config_store.load_config()
    question = (question or "").strip()
    if not question:
        raise ValueError("question must not be empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question exceeds {MAX_QUESTION_CHARS} chars")

    rows, window_hours, mentioned = filter_sightings(
        question, now=now, sightings=sightings, pets=pets,
    )
    pets = pets if pets is not None else PetStore().load()
    system, user = compose_prompt(
        question, rows, pets=pets, window_hours=window_hours,
        mentioned=mentioned, lang=lang,
    )

    backend = pet_diary.active_backend(cfg)
    if backend is None:
        raise pet_diary.DiaryNotConfigured(
            "no LLM backend configured for query"
        )

    # Inflight dedupe — see _query_inflight comment above. We don't
    # block the second caller; we raise a clear "in flight" error so
    # the UI can debounce the button. Cleaner than coalescing on the
    # answer (would tie two sessions' user prompts together).
    key = _question_key(question)
    async with _query_inflight_lock:
        if key in _query_inflight:
            raise RuntimeError("query already in flight for this question")
        _query_inflight.add(key)

    try:
        if backend == "ollama":
            caller = ollama_caller or pet_diary._call_ollama
            text = await caller(cfg.ollama_base_url, system, user,
                                  model=cfg.ollama_model or "qwen2.5:3b")
        elif backend == "openai":
            caller = openai_caller or pet_diary._call_openai
            text = await caller(cfg.openai_api_key, system, user)
        else:  # pro_relay
            caller = relay_caller or pet_diary._call_pro_relay
            text = await caller(cfg.pawcorder_pro_license_key,
                                  system, user, lang=lang)
    except Exception as exc:
        _query_inflight.discard(key)
        reliability.record(
            "ai_inference", f"query:{backend}", "fail",
            message=str(exc)[:200],
        )
        raise
    _query_inflight.discard(key)

    reliability.record("ai_inference", f"query:{backend}", "ok")

    valid_ids = {str(r.get("event_id") or "") for r in rows}
    valid_ids.discard("")
    event_ids = extract_event_ids(text, valid_ids)
    answer_text = strip_events_line(text)

    return QueryAnswer(
        question=question, answer=answer_text,
        event_ids=event_ids, backend=backend,
        samples_considered=len(rows), window_hours=window_hours,
    )
