"""API key auth — alongside the cookie-based admin session.

For programmatic integrations (Home Assistant custom component, an
iOS Shortcuts app, scripted backups, future native iOS app) cookies
are awkward. API keys are: long random tokens, sent in the
`Authorization: Bearer <key>` header, optional name + revocable.

We store hashes only — `os.urandom(32)` token, sha256 stored. The
plain key is shown to the user exactly once when created. Revocation
removes the hash; the previously-issued key stops working.

Bearer auth bypasses CSRF because:
  - The header is custom, browsers can't forge it cross-origin without
    CORS (we don't grant CORS on key-protected routes either).
  - Even if forged, an attacker would need to know the bearer key
    itself, which is the single secret the integration carries.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Request

from .utils import atomic_write_text

logger = logging.getLogger("pawcorder.api_keys")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
KEYS_FILE = DATA_DIR / "config" / "api_keys.json"

KEY_PREFIX = "pwc_"      # so users can spot a pawcorder key in their config
KEY_BYTES = 32           # → 64-char hex; ample entropy
HASH_PREFIX_LEN = 8      # display the first 8 chars of the hash for ID


@dataclass
class ApiKeyRecord:
    """One row in api_keys.json. We never store the plain key."""
    key_id: str             # first 8 chars of sha256 — stable identifier
    name: str               # user-friendly label
    sha256_hex: str         # full hash, used for verification
    created_at: int = 0     # unix seconds
    last_used_at: int = 0   # unix seconds, 0 if never

    def to_public_dict(self) -> dict:
        """The shape returned to UI — never includes the actual hash."""
        return {
            "key_id": self.key_id,
            "name": self.name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "preview": f"{KEY_PREFIX}…{self.key_id}",
        }

    @staticmethod
    def from_dict(d: dict) -> "ApiKeyRecord":
        return ApiKeyRecord(
            key_id=str(d.get("key_id") or ""),
            name=str(d.get("name") or ""),
            sha256_hex=str(d.get("sha256_hex") or ""),
            created_at=int(d.get("created_at") or 0),
            last_used_at=int(d.get("last_used_at") or 0),
        )


# ---- store -------------------------------------------------------------

def _load() -> list[ApiKeyRecord]:
    if not KEYS_FILE.exists():
        return []
    try:
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("api_keys.json malformed; treating as empty")
        return []
    items = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[ApiKeyRecord] = []
    for entry in items:
        if isinstance(entry, dict) and entry.get("sha256_hex"):
            out.append(ApiKeyRecord.from_dict(entry))
    return out


def _save(records: list[ApiKeyRecord]) -> None:
    payload = {"keys": [vars(r) for r in records]}
    atomic_write_text(KEYS_FILE, json.dumps(payload, indent=2))


# ---- public API --------------------------------------------------------

def list_keys() -> list[ApiKeyRecord]:
    return _load()


def list_keys_public() -> list[dict]:
    """For /api/system/api-keys — no hash leaks."""
    return [r.to_public_dict() for r in _load()]


def create_key(name: str) -> tuple[str, ApiKeyRecord]:
    """Mint a new key. Returns (plain_key, record). The plain key is
    shown to the user once — we keep only the hash."""
    name = (name or "").strip()[:64] or "unnamed"
    raw = secrets.token_urlsafe(KEY_BYTES)
    plain = f"{KEY_PREFIX}{raw}"
    digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    record = ApiKeyRecord(
        key_id=digest[:HASH_PREFIX_LEN],
        name=name,
        sha256_hex=digest,
        created_at=_now(),
    )
    records = _load()
    records.append(record)
    _save(records)
    return plain, record


def revoke_key(key_id: str) -> bool:
    records = _load()
    new = [r for r in records if r.key_id != key_id]
    if len(new) == len(records):
        return False
    _save(new)
    return True


def verify_bearer(token: str) -> Optional[ApiKeyRecord]:
    """Look up a presented token. Returns the matching record if
    verified, None otherwise. Side-effect: bumps last_used_at on a hit."""
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    records = _load()
    for r in records:
        if secrets_compare(r.sha256_hex, digest):
            r.last_used_at = _now()
            _save(records)
            return r
    return None


def secrets_compare(a: str, b: str) -> bool:
    """Constant-time string compare — same shape as hmac.compare_digest
    but works on arbitrary str."""
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _now() -> int:
    import time
    return int(time.time())


# ---- request-side helper ----------------------------------------------

def from_request(request: Request) -> Optional[ApiKeyRecord]:
    """Parse `Authorization: Bearer <token>` and verify. Returns the
    record on success, None on missing / invalid."""
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return verify_bearer(parts[1].strip())
