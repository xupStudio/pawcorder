"""Browser Web Push (VAPID, RFC 8030).

What this gives the user:
  - On Chrome / Edge / Firefox / Safari (desktop): native banner pop
    when pawcorder detects a pet, even if the pawcorder tab is closed.
  - On iOS Safari (PWA installed to home screen, iOS 16.4+): same.
  - On Android Chrome (PWA): same.

  - On iOS Safari (NOT installed to home screen): browser refuses
    to subscribe. UI banner falls back to Telegram in that case.

We generate a VAPID key pair on first boot, store it in
config/webpush_vapid.json, and reuse it forever (subscribers
remember the public key — rotating breaks every existing browser
subscription). Subscription records live in webpush_subs.json.

Soft-fails: if pywebpush isn't installed, every send is a no-op.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .utils import atomic_write_text

logger = logging.getLogger("pawcorder.webpush")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
VAPID_FILE = DATA_DIR / "config" / "webpush_vapid.json"
SUBS_FILE = DATA_DIR / "config" / "webpush_subs.json"

# VAPID 'sub' field — required by spec, must be a mailto: or https://.
# We don't actually receive bounces; this is a contact for push
# providers if they need to talk to us.
VAPID_CLAIM_EMAIL = os.environ.get("PAWCORDER_VAPID_EMAIL", "mailto:admin@pawcorder.local")


@dataclass
class VapidKeyPair:
    public_key_b64: str   # URL-safe base64, no padding
    private_key_pem: str  # full PEM string

    def to_dict(self) -> dict:
        return {
            "public_key_b64": self.public_key_b64,
            "private_key_pem": self.private_key_pem,
        }

    @staticmethod
    def from_dict(d: dict) -> "VapidKeyPair":
        return VapidKeyPair(
            public_key_b64=str(d.get("public_key_b64") or ""),
            private_key_pem=str(d.get("private_key_pem") or ""),
        )


@dataclass
class Subscription:
    """One browser subscription, as POSTed by /api/webpush/subscribe."""
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str = ""
    created_at: int = 0


def _now() -> int:
    import time
    return int(time.time())


# ---- VAPID keypair management -----------------------------------------

def load_or_create_keypair() -> Optional[VapidKeyPair]:
    """Read keypair from disk. Generates a new one if missing.
    Returns None when cryptography / pywebpush isn't installed —
    callers should treat that as 'web push disabled'."""
    if VAPID_FILE.exists():
        try:
            data = json.loads(VAPID_FILE.read_text(encoding="utf-8"))
            return VapidKeyPair.from_dict(data)
        except (OSError, json.JSONDecodeError):
            logger.warning("vapid file unreadable; regenerating")

    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        logger.info("cryptography not installed — web push disabled")
        return None

    private = ec.generate_private_key(ec.SECP256R1(), default_backend())
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    # Public key in raw uncompressed form, then URL-safe base64 — what
    # the browser PushManager.subscribe applicationServerKey wants.
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(public_raw).rstrip(b"=").decode("ascii")

    pair = VapidKeyPair(public_key_b64=public_b64, private_key_pem=private_pem)
    VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(VAPID_FILE, json.dumps(pair.to_dict(), indent=2))
    logger.info("generated new VAPID keypair")
    return pair


def public_key_b64() -> str:
    """For the JS subscriber. Empty string means push disabled."""
    pair = load_or_create_keypair()
    return pair.public_key_b64 if pair else ""


# ---- subscription store -----------------------------------------------

def list_subscriptions() -> list[Subscription]:
    if not SUBS_FILE.exists():
        return []
    try:
        data = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("subs") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[Subscription] = []
    for entry in items:
        if isinstance(entry, dict) and entry.get("endpoint"):
            out.append(Subscription(
                endpoint=entry["endpoint"],
                p256dh=str(entry.get("p256dh") or ""),
                auth=str(entry.get("auth") or ""),
                user_agent=str(entry.get("user_agent") or ""),
                created_at=int(entry.get("created_at") or 0),
            ))
    return out


def _save_subscriptions(subs: list[Subscription]) -> None:
    payload = {"subs": [vars(s) for s in subs]}
    SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(SUBS_FILE, json.dumps(payload, indent=2))


def add_subscription(endpoint: str, p256dh: str, auth: str, *,
                     user_agent: str = "") -> Subscription:
    """Append + dedupe by endpoint. Browsers send the same endpoint on
    re-subscribe; we treat that as an update."""
    sub = Subscription(
        endpoint=endpoint, p256dh=p256dh, auth=auth,
        user_agent=user_agent[:200], created_at=_now(),
    )
    subs = [s for s in list_subscriptions() if s.endpoint != endpoint]
    subs.append(sub)
    _save_subscriptions(subs)
    return sub


def remove_subscription(endpoint: str) -> bool:
    subs = list_subscriptions()
    new = [s for s in subs if s.endpoint != endpoint]
    if len(new) == len(subs):
        return False
    _save_subscriptions(new)
    return True


# ---- send -------------------------------------------------------------

def send_to_all(title: str, body: str, *, url: str = "/") -> dict:
    """Fire push to every stored subscription. Bad endpoints (410
    Gone — user revoked) are auto-pruned. Returns {sent, pruned, errors}.
    """
    subs = list_subscriptions()
    if not subs:
        return {"sent": 0, "pruned": 0, "errors": 0}
    pair = load_or_create_keypair()
    if not pair:
        return {"sent": 0, "pruned": 0, "errors": 0, "skipped": "vapid_unavailable"}
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return {"sent": 0, "pruned": 0, "errors": 0, "skipped": "pywebpush_unavailable"}

    sent = pruned = errors = 0
    keep: list[Subscription] = []
    payload = json.dumps({"title": title, "body": body, "url": url})
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s.endpoint,
                    "keys": {"p256dh": s.p256dh, "auth": s.auth},
                },
                data=payload,
                vapid_private_key=pair.private_key_pem,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL},
                ttl=3600,
            )
            sent += 1
            keep.append(s)
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None) if response else None
            if status in (404, 410):
                # Subscription dead; drop it.
                pruned += 1
                continue
            errors += 1
            keep.append(s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("push send to %s failed: %s", s.endpoint[:60], exc)
            errors += 1
            keep.append(s)

    if pruned > 0:
        _save_subscriptions(keep)
    return {"sent": sent, "pruned": pruned, "errors": errors}
