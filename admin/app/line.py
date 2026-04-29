"""LINE Messaging API integration.

LINE Notify (the simpler webhook-style service) was shut down on
2025-03-31. The replacement is the LINE Messaging API, which requires
a Channel Access Token and a destination user/group ID.

LINE rejects direct image uploads — image messages need a publicly
reachable URL. Self-hosted pawcorder behind a home NAT can't easily
provide that, so this module sends text-only messages.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

LINE_API_BASE = "https://api.line.me/v2"


@dataclass
class LineSendResult:
    ok: bool
    error: Optional[str] = None


async def _push(token: str, body: dict, timeout: float = 12.0) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{LINE_API_BASE}/bot/message/push", json=body, headers=headers)
    if resp.status_code != 200:
        try:
            payload = resp.json()
            msg = payload.get("message") or resp.text
        except ValueError:
            msg = resp.text
        raise RuntimeError(f"LINE API {resp.status_code}: {msg}")


async def send_text(token: str, to: str, text: str) -> None:
    await _push(token, {"to": to, "messages": [{"type": "text", "text": text[:5000]}]})


async def send_test(token: str, to: str) -> LineSendResult:
    try:
        await send_text(token, to, "pawcorder test message — your LINE setup works.")
        return LineSendResult(ok=True)
    except Exception as exc:  # noqa: BLE001
        return LineSendResult(ok=False, error=str(exc))
