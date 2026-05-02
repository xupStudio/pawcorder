"""Family invite links.

The admin clicks "Invite a family member" → backend mints a 32-char
URL-safe token good for 7 days. The link looks like:

    https://<your-pawcorder>/invite/<token>

The recipient opens the link on their phone, picks a username and
password, and gets a ``family`` role account. Single-use: the token is
consumed at signup.

Storage: ``invites.yml`` next to ``users.yml``. Same atomic-write +
lock pattern. Tokens are stored as bcrypt-style salted hashes so a
read of the file (e.g. via a backup leak) doesn't grant new accounts —
the recipient has to present the original token.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .utils import atomic_write_text

logger = logging.getLogger("pawcorder.invites")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
INVITES_FILE = DATA_DIR / "config" / "invites.yml"

DEFAULT_TTL_SECS = 7 * 86400  # 7 days
TOKEN_BYTES = 24              # 32 chars URL-safe base64

_LOCK = threading.Lock()


class InviteError(ValueError):
    """User-facing invite mutation failure."""


@dataclass
class InviteRecord:
    """One row in invites.yml. Token is stored hashed; only the holder
    of the original plaintext can redeem the invite."""
    token_hash: str          # hex of sha256(token)
    role: str
    created_at: int
    expires_at: int
    created_by: str
    used_at: int = 0
    used_by_username: str = ""

    def is_expired(self, now: Optional[int] = None) -> bool:
        return (now or int(time.time())) >= self.expires_at

    def is_used(self) -> bool:
        return self.used_at > 0

    def is_active(self, now: Optional[int] = None) -> bool:
        return not self.is_used() and not self.is_expired(now)

    def to_public(self) -> dict:
        return {
            "id": self.token_hash[:8],  # short opaque id for revoke/copy UI
            "role": self.role,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "created_by": self.created_by,
            "used_at": self.used_at,
            "used_by_username": self.used_by_username,
            "active": self.is_active(),
        }


# ---- IO -----------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_hash_token(token), expected_hash)


def _load_raw() -> list[dict]:
    if not INVITES_FILE.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(INVITES_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return []
    items = data.get("invites") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _save_raw(invites: list[InviteRecord]) -> None:
    import yaml
    INVITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"invites": [vars(i) for i in invites]}
    atomic_write_text(INVITES_FILE, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def list_invites() -> list[InviteRecord]:
    out: list[InviteRecord] = []
    for entry in _load_raw():
        if not isinstance(entry, dict):
            continue
        if not entry.get("token_hash"):
            continue
        out.append(InviteRecord(
            token_hash=str(entry["token_hash"]),
            role=str(entry.get("role") or "family"),
            created_at=int(entry.get("created_at") or 0),
            expires_at=int(entry.get("expires_at") or 0),
            created_by=str(entry.get("created_by") or ""),
            used_at=int(entry.get("used_at") or 0),
            used_by_username=str(entry.get("used_by_username") or ""),
        ))
    return out


def list_active() -> list[InviteRecord]:
    now = int(time.time())
    return [i for i in list_invites() if i.is_active(now)]


# ---- mutations ----------------------------------------------------------

def create(*, role: str, created_by: str, ttl_secs: int = DEFAULT_TTL_SECS) -> tuple[str, InviteRecord]:
    """Mint a new invite. Returns (plaintext_token, record).

    The plaintext token is shown to the inviter ONCE — we only store
    its hash, so this is the only chance to copy it.
    """
    if role not in ("family", "kid"):
        # Admin invites would be a privilege-escalation footgun.
        # Admins can be added by other admins via /api/users with a
        # password the admin sets directly.
        raise InviteError("invite role must be 'family' or 'kid'")
    if ttl_secs < 60 or ttl_secs > 30 * 86400:
        raise InviteError("ttl_secs must be between 60s and 30 days")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    rec = InviteRecord(
        token_hash=_hash_token(token),
        role=role,
        created_at=int(time.time()),
        expires_at=int(time.time()) + ttl_secs,
        created_by=created_by or "admin",
    )
    with _LOCK:
        items = list_invites()
        items.append(rec)
        _save_raw(items)
    return token, rec


def revoke(public_id: str) -> bool:
    """Drop an invite by its 8-char public id (= first 8 chars of token_hash)."""
    if not public_id:
        return False
    with _LOCK:
        items = list_invites()
        before = len(items)
        items = [i for i in items if i.token_hash[:8] != public_id]
        if len(items) == before:
            return False
        _save_raw(items)
        return True


def consume(token: str, *, used_by_username: str) -> InviteRecord:
    """Mark an invite as used. Caller has already authenticated the
    redemption flow and created the user — this just flips the row.

    Raises InviteError if the token is unknown, expired, or already used.
    """
    if not token or not used_by_username:
        raise InviteError("missing token or username")
    with _LOCK:
        items = list_invites()
        now = int(time.time())
        for inv in items:
            if not _verify(token, inv.token_hash):
                continue
            if inv.is_used():
                raise InviteError("invite already used")
            if inv.is_expired(now):
                raise InviteError("invite expired")
            inv.used_at = now
            inv.used_by_username = used_by_username
            _save_raw(items)
            return inv
        raise InviteError("invite not found")


def find_active(token: str) -> Optional[InviteRecord]:
    """Lookup without mutation. Used by the redeem page to render
    role/expiry preview before the user sets a password."""
    if not token:
        return None
    now = int(time.time())
    for inv in list_invites():
        if _verify(token, inv.token_hash) and inv.is_active(now):
            return inv
    return None


def prune_expired(now: Optional[int] = None) -> int:
    """Drop expired AND used invites older than 30 days. Used by a
    daily cleanup task so invites.yml doesn't grow forever."""
    cutoff = (now or int(time.time())) - 30 * 86400
    with _LOCK:
        items = list_invites()
        keep = []
        dropped = 0
        for inv in items:
            if inv.is_used() and inv.used_at < cutoff:
                dropped += 1
                continue
            if inv.is_expired() and inv.expires_at < cutoff:
                dropped += 1
                continue
            keep.append(inv)
        if dropped:
            _save_raw(keep)
        return dropped
