"""Telegram bot integration.

Two responsibilities:
  1. Send arbitrary messages and photos to a Telegram chat (used by the
     "send test message" button on the /notifications page).
  2. Run a background loop that polls Frigate's /api/events and forwards
     new pet detections to Telegram.

Frigate has no built-in Telegram support, so we tail its events API. This
runs inside the admin container — no extra service, no MQTT broker.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config_store, line as line_api

logger = logging.getLogger("pawcorder.telegram")

FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "http://frigate:5000")
POLL_INTERVAL_SECONDS = 8
EVENT_LABELS = ("cat", "dog")


@dataclass
class TelegramSendResult:
    ok: bool
    error: Optional[str] = None


# ---- low-level Telegram client -----------------------------------------

async def _api_call(token: str, method: str, *, data: dict | None = None,
                    files: dict | None = None, timeout: float = 15.0) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, data=data or {}, files=files)
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or f"telegram {method} failed")
    return payload["result"]


async def send_message(token: str, chat_id: str, text: str) -> None:
    await _api_call(token, "sendMessage", data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


async def send_photo(token: str, chat_id: str, photo_bytes: bytes, caption: str) -> None:
    await _api_call(
        token, "sendPhoto",
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files={"photo": ("snapshot.jpg", photo_bytes, "image/jpeg")},
    )


async def send_test(token: str, chat_id: str) -> TelegramSendResult:
    try:
        await send_message(token, chat_id, "<b>pawcorder</b> test message — your Telegram setup works.")
        return TelegramSendResult(ok=True)
    except Exception as exc:  # noqa: BLE001
        return TelegramSendResult(ok=False, error=str(exc))


# ---- Frigate event poller ----------------------------------------------

class FrigateEventPoller:
    """Background asyncio task that polls Frigate's events API.

    Tracks the last seen `start_time` to avoid duplicates. On each new
    event whose label is `cat` or `dog`, downloads the snapshot and sends
    it to the configured Telegram chat.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # In-memory checkpoint. Survives across reload but not across container restart;
        # at startup we initialize to "now" so we don't replay history.
        self._last_seen: float = time.time()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="telegram-poller")
            logger.info("telegram poller started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("telegram poller stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                logger.warning("telegram poller tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        cfg = config_store.load_config()
        telegram_on = cfg.telegram_enabled and cfg.telegram_bot_token and cfg.telegram_chat_id
        line_on = cfg.line_enabled and cfg.line_channel_token and cfg.line_target_id
        if not (telegram_on or line_on):
            return

        events = await self._fetch_events()
        for event in events:
            label = event.get("label")
            start_time = event.get("start_time") or 0
            if label not in EVENT_LABELS:
                continue
            if start_time <= self._last_seen:
                continue
            await self._notify(cfg, event, telegram_on=telegram_on, line_on=line_on)
            self._last_seen = max(self._last_seen, start_time)

    async def _fetch_events(self) -> list[dict]:
        url = f"{FRIGATE_BASE_URL}/api/events"
        params = {"after": int(self._last_seen), "limit": 25, "include_thumbnails": 0}
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            # Sort ascending so our checkpoint advances monotonically.
            data.sort(key=lambda e: e.get("start_time", 0))
            return data
        except (httpx.HTTPError, ValueError):
            return []

    async def _notify(self, cfg: config_store.Config, event: dict,
                      *, telegram_on: bool, line_on: bool) -> None:
        camera = event.get("camera", "unknown")
        label = event.get("label", "?")
        score = event.get("top_score") or event.get("score") or 0.0
        event_id = event.get("id")

        # Try to identify which of the user's pets this is. Soft-fails if
        # recognition isn't enabled / model not loaded — the event still
        # gets sent, just without the pet name.
        snapshot = await self._fetch_snapshot(event_id) if event_id else None
        pet_label = ""
        if snapshot and event_id:
            try:
                from . import recognition
                match = recognition.identify_event(
                    snapshot,
                    event_id=str(event_id),
                    camera=camera,
                    label=label,
                    start_time=float(event.get("start_time") or 0),
                    end_time=float(event.get("end_time") or 0),
                )
                if match.pet_name:
                    marker = "" if match.confidence == "high" else " (?)"
                    pet_label = f"{match.pet_name}{marker}"
            except Exception as exc:  # noqa: BLE001
                logger.debug("recognition skipped for event %s: %s", event_id, exc)

        # Caption: lead with the pet name when known, fall back to the
        # generic species label otherwise.
        if pet_label:
            caption_html = (
                f"<b>{pet_label}</b> ({label}) in <b>{camera}</b>\n"
                f"score: {float(score):.2f}"
            )
            caption_plain = f"{pet_label} ({label}) in {camera} (score {float(score):.2f})"
        else:
            caption_html = (
                f"<b>{label}</b> in <b>{camera}</b>\n"
                f"score: {float(score):.2f}"
            )
            caption_plain = f"{label} in {camera} (score {float(score):.2f})"

        if telegram_on:
            try:
                if snapshot:
                    await send_photo(cfg.telegram_bot_token, cfg.telegram_chat_id, snapshot, caption_html)
                else:
                    await send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, caption_html)
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram send failed for event %s: %s", event_id, exc)

        if line_on:
            # LINE Messaging API can't accept inline image uploads; text-only.
            try:
                await line_api.send_text(cfg.line_channel_token, cfg.line_target_id, caption_plain)
            except Exception as exc:  # noqa: BLE001
                logger.warning("line send failed for event %s: %s", event_id, exc)

        # Web Push — runs in parallel with Telegram / LINE so users can
        # pick whichever they actually have configured. Soft-fails when
        # there are 0 subscribers or pywebpush isn't installed.
        try:
            from . import webpush
            # Run in executor — pywebpush is sync I/O.
            import asyncio
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                webpush.send_to_all,
                pet_label or label,
                f"in {camera}",
                f"/cameras",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("webpush skipped for event %s: %s", event_id, exc)

    async def _fetch_snapshot(self, event_id: str) -> bytes | None:
        url = f"{FRIGATE_BASE_URL}/api/events/{event_id}/snapshot.jpg"
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url, params={"bbox": 1})
            if resp.status_code == 200 and resp.content:
                return resp.content
        except httpx.HTTPError:
            pass
        return None


poller = FrigateEventPoller()
