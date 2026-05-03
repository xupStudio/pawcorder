"""Telegram chat-ID auto-discovery via bot deep-link pairing.

The /notifications page used to ask users to:
  1. Paste a bot token (unavoidable — Telegram has no user-OAuth-for-bots).
  2. Find their numeric chat ID by messaging @userinfobot in Telegram and
     copying the number back.

Step 2 is the painful one. This module replaces it with a pairing dance:

  1. UI calls ``start_pairing(token)`` which returns:
       - ``bot_username`` (so we can build the deep link)
       - ``pairing_code`` (random, recorded in-memory)
       - ``deep_link`` ("https://t.me/<bot>?start=<code>")
  2. UI shows a QR + clickable button. User taps it.
  3. Telegram opens, user taps "Start" → bot receives ``/start <code>``.
  4. Backend's poll loop (already running for sightings) calls
     ``check_pairing(token)`` which polls ``getUpdates`` and matches
     ``/start <code>`` against the pending codes — capturing the sender's
     chat_id and saving it to config.

State lives in-process (a dict keyed on pairing_code) — the codes are
short-lived (5 min). We don't persist them; if the admin restarts
mid-pairing the user just clicks Generate again.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("pawcorder.telegram_pairing")

PAIRING_TTL_SECONDS = 300  # 5 min — Telegram start params expire on user side too


@dataclass
class _PendingPairing:
    code: str
    bot_token: str
    bot_username: str
    expires_at: float


# pairing_code → _PendingPairing. Keyed on code (not token) so multiple
# admins testing on the same bot can pair concurrently.
_pending: dict[str, _PendingPairing] = {}


def _now() -> float: return time.time()


def _gc() -> None:
    """Drop expired entries. Called from start/check, not on a timer."""
    cutoff = _now()
    stale = [k for k, v in _pending.items() if v.expires_at < cutoff]
    for k in stale:
        _pending.pop(k, None)


@dataclass
class PairingStart:
    pairing_code: str
    bot_username: str
    deep_link: str
    expires_in: int


async def get_bot_username(token: str) -> str:
    """Resolve the bot's @username via getMe. Required for deep-link
    construction. Raises on invalid token."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url)
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or "getMe failed")
    username = (payload.get("result") or {}).get("username") or ""
    if not username:
        raise RuntimeError("bot has no username — set one via @BotFather")
    return username


async def start_pairing(token: str) -> PairingStart:
    """Mint a new pairing code, return the deep-link the UI should display."""
    _gc()
    if not token:
        raise ValueError("bot token required")
    username = await get_bot_username(token)
    # 12 chars from base32 — short enough to fit in Telegram's 64-char
    # start param after the "pcr-" prefix; 60 bits of entropy is fine
    # because the window is 5 min.
    suffix = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(12))
    code = f"pcr-{suffix}"
    _pending[code] = _PendingPairing(
        code=code, bot_token=token, bot_username=username,
        expires_at=_now() + PAIRING_TTL_SECONDS,
    )
    deep_link = f"https://t.me/{username}?start={code}"
    return PairingStart(
        pairing_code=code, bot_username=username,
        deep_link=deep_link, expires_in=PAIRING_TTL_SECONDS,
    )


@dataclass
class PairingResult:
    matched: bool
    chat_id: str = ""
    pairing_code: str = ""        # which code this update consumed


async def check_pairing(token: str, *, last_update_id: Optional[int] = None
                         ) -> tuple[Optional[PairingResult], int]:
    """Poll ``getUpdates`` for any ``/start <code>`` matching a pending code.

    Returns ``(result, new_last_update_id)``. ``result`` is None when no
    pending code matched any update — the caller updates its checkpoint
    regardless so it doesn't re-process the same updates.

    Designed to be called from the same poll loop that already polls
    Frigate events — no extra timer thread.
    """
    _gc()
    if not _pending:
        return None, last_update_id or 0

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params: dict = {"timeout": 0, "allowed_updates": '["message"]'}
    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("getUpdates failed: %s", exc)
        return None, last_update_id or 0

    if not data.get("ok"):
        return None, last_update_id or 0

    updates = data.get("result") or []
    if not updates:
        return None, last_update_id or 0

    new_last = max((u.get("update_id") or 0) for u in updates)
    matched: Optional[PairingResult] = None

    for upd in updates:
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not text.startswith("/start ") or not chat_id:
            continue
        code = text.split(" ", 1)[1].strip()
        if code in _pending:
            matched = PairingResult(matched=True, chat_id=str(chat_id),
                                     pairing_code=code)
            _pending.pop(code, None)
            # Keep iterating in case multiple pairings completed in the
            # same poll batch — but for our caller's "first match wins"
            # contract, we break here. Future-proof: if we ever need
            # multi-pair, return a list.
            break

    return matched, new_last
