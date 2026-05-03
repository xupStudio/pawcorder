"""ntfy.sh push notifications — zero-token, zero-account.

The user picks a random topic, subscribes from the ntfy app on their
phone, and Pawcorder POSTs notifications to ``$NTFY_SERVER/$TOPIC``.

Compared to Telegram / LINE this is the simplest possible setup:
  - No bot to create
  - No chat / channel ID to find
  - No vendor account to register
  - Works on iOS via ntfy.sh's APNs relay (free tier, generous limits)
  - Self-hostable for full data sovereignty

Topic security: anyone who knows the topic URL can publish AND subscribe.
We generate 32 chars of base32 entropy → ~160 bits, unguessable in
practice. We don't bother with end-to-end encryption — pet detection
events aren't sensitive enough to warrant the UX hit (users would have
to share an encryption key with the ntfy app, which the app doesn't
support natively).
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

import httpx

logger = logging.getLogger("pawcorder.ntfy")

# Base32 without padding — URL-safe, 32 chars = 160 bits of entropy.
_TOPIC_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def generate_topic(prefix: str = "pawcorder-") -> str:
    """Random unguessable topic. The prefix helps users recognise their
    own topic in a list; the suffix is the secret part."""
    suffix = "".join(secrets.choice(_TOPIC_ALPHABET) for _ in range(32))
    return f"{prefix}{suffix}"


@dataclass
class SendResult:
    ok: bool
    status_code: int = 0
    error: str = ""


async def send(server: str, topic: str, *, title: str, body: str,
                priority: int = 3, tags: list[str] | None = None) -> SendResult:
    """Push one notification. ntfy uses HTTP POST with the message body
    + headers for metadata. Priority 1-5; tags are emoji shortcodes."""
    if not topic or not server:
        return SendResult(ok=False, error="ntfy not configured")
    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "ignore"),
        "Priority": str(max(1, min(5, priority))),
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=body.encode("utf-8"), headers=headers)
        return SendResult(ok=resp.status_code < 400, status_code=resp.status_code)
    except httpx.HTTPError as exc:
        return SendResult(ok=False, error=str(exc)[:200])


async def send_test(server: str, topic: str) -> SendResult:
    """The "Send test" button calls this. The user expects to see a push
    immediately on their phone if the ntfy app is subscribed."""
    return await send(
        server, topic,
        title="Pawcorder test",
        body="If you see this on your phone, ntfy is configured.",
        priority=3,
        tags=["paw_prints"],
    )
