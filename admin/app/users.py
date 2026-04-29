"""Multi-user with role-based access.

Three roles, picked to match the actual mental model of a household:
  - admin: full access. Camera CRUD, pet CRUD, system, uninstall, etc.
  - family: read everything, change notifications + pets, watch live.
            Cannot delete cameras, change passwords, run uninstall.
  - kid: read-only on cameras + pets dashboard. Cannot see /system,
         /backup, /privacy, notification config, uninstall.

Backwards compatibility: if users.yml doesn't exist, the legacy
ADMIN_PASSWORD path still works and the resulting session is
treated as 'admin' role. First-time multi-user setup migrates the
legacy password into a default 'admin' user named 'admin'.

Storage: bcrypt-style salted hash via PBKDF2-HMAC-SHA256 (so we
don't pull bcrypt as a dep — pywebpush's cryptography would
work too but stdlib is simpler).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import Request

from .utils import atomic_write_text

logger = logging.getLogger("pawcorder.users")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
USERS_FILE = DATA_DIR / "config" / "users.yml"

ROLES = ("admin", "family", "kid")
ROLE_RANK = {"admin": 3, "family": 2, "kid": 1}

# Serializes read-modify-write of users.yml. Without this two
# concurrent authenticate() calls (or auth + create_user) can race
# and clobber each other's last_login_at / new user, since we re-read
# inside the closure. Pure stdlib threading lock — fine for the
# single-process FastAPI we ship.
_USERS_LOCK = threading.Lock()

# Per-route minimum role. The default for any route not listed is
# 'family' — most routes (read endpoints, common writes) are fine
# for family. Lock down only what genuinely matters.
#
# We don't enforce these at the FastAPI router level — the route
# handler calls require_role() with the appropriate level.
ROLE_REQUIREMENTS = {
    "admin": (
        "system_settings", "uninstall", "user_management",
        "delete_camera", "change_password", "api_keys",
        "backup_restore", "energy_mode", "shell_access",
    ),
    "family": (
        "view_cameras", "watch_live", "edit_pets", "configure_notifications",
        "view_health", "watch_highlights", "view_zones", "view_timeline",
    ),
    "kid": ("view_cameras", "watch_live", "view_pets_dashboard"),
}


@dataclass
class UserRecord:
    """One row in users.yml."""
    username: str
    role: str
    salt: str               # hex
    pw_hash: str            # PBKDF2(SHA-256, password, salt, 200_000) hex
    created_at: int = 0
    last_login_at: int = 0

    def to_public(self) -> dict:
        return {
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


# ---- hashing -----------------------------------------------------------

def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000,
    ).hex()


def _verify(password: str, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_hash(password, salt), expected_hash)


# ---- IO ----------------------------------------------------------------

def _load_raw() -> list[dict]:
    if not USERS_FILE.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(USERS_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return []
    items = data.get("users") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _save_raw(users: list[UserRecord]) -> None:
    import yaml
    payload = {"users": [vars(u) for u in users]}
    atomic_write_text(USERS_FILE, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def list_users() -> list[UserRecord]:
    out: list[UserRecord] = []
    for entry in _load_raw():
        if not isinstance(entry, dict):
            continue
        if not entry.get("username") or not entry.get("pw_hash"):
            continue
        out.append(UserRecord(
            username=str(entry["username"]),
            role=str(entry.get("role") or "family"),
            salt=str(entry.get("salt") or ""),
            pw_hash=str(entry["pw_hash"]),
            created_at=int(entry.get("created_at") or 0),
            last_login_at=int(entry.get("last_login_at") or 0),
        ))
    return out


def get_user(username: str) -> Optional[UserRecord]:
    for u in list_users():
        if u.username == username:
            return u
    return None


def has_users() -> bool:
    """True iff users.yml exists with at least one record. When False,
    we fall back to the legacy single-password ADMIN_PASSWORD path."""
    return any(list_users())


# ---- mutations ---------------------------------------------------------

class UserError(ValueError):
    pass


def create_user(username: str, password: str, role: str) -> UserRecord:
    username = (username or "").strip()
    if not username or len(username) > 32 or not username.replace("_", "").replace("-", "").isalnum():
        raise UserError("username must be 1-32 chars, alphanumeric / underscore / dash only")
    if role not in ROLES:
        raise UserError(f"role must be one of {ROLES}")
    if not password or len(password) < 8:
        raise UserError("password must be at least 8 characters")

    with _USERS_LOCK:
        users = list_users()
        if any(u.username == username for u in users):
            raise UserError(f"user {username!r} already exists")

        salt = secrets.token_hex(16)
        user = UserRecord(
            username=username, role=role, salt=salt,
            pw_hash=_hash(password, salt), created_at=int(time.time()),
        )
        users.append(user)
        _save_raw(users)
        return user


def delete_user(username: str) -> bool:
    with _USERS_LOCK:
        users = list_users()
        new = [u for u in users if u.username != username]
        if len(new) == len(users):
            return False
        # Refuse to leave 0 admins — would lock everyone out.
        if not any(u.role == "admin" for u in new):
            raise UserError("cannot delete the last admin")
        _save_raw(new)
        return True


def change_password(username: str, new_password: str) -> bool:
    if not new_password or len(new_password) < 8:
        raise UserError("password must be at least 8 characters")
    with _USERS_LOCK:
        users = list_users()
        for u in users:
            if u.username == username:
                u.salt = secrets.token_hex(16)
                u.pw_hash = _hash(new_password, u.salt)
                _save_raw(users)
                return True
        return False


def change_role(username: str, new_role: str) -> bool:
    if new_role not in ROLES:
        raise UserError(f"role must be one of {ROLES}")
    with _USERS_LOCK:
        users = list_users()
        target = next((u for u in users if u.username == username), None)
        if not target:
            return False
        # Block demoting the last admin.
        if target.role == "admin" and new_role != "admin":
            admins = [u for u in users if u.role == "admin"]
            if len(admins) <= 1:
                raise UserError("cannot demote the last admin")
        target.role = new_role
        _save_raw(users)
        return True


def authenticate(username: str, password: str) -> Optional[UserRecord]:
    """Verify credentials. On success, bumps last_login_at.

    Take the lock around the read-modify-write so a second auth
    call (or a concurrent create_user) doesn't see a stale list and
    overwrite a fresher last_login_at.
    """
    with _USERS_LOCK:
        user = get_user(username)
        if not user:
            return None
        if not _verify(password, user.salt, user.pw_hash):
            return None
        user.last_login_at = int(time.time())
        users = [u if u.username != user.username else user for u in list_users()]
        _save_raw(users)
        return user


# ---- session helpers ---------------------------------------------------

def role_from_request(request: Request) -> Optional[str]:
    """Pull role out of the session cookie. Returns None if not
    authenticated. Returns 'admin' for legacy single-password sessions
    so the existing flow still works.
    """
    from . import auth, api_keys
    # API key bearers are always treated as 'admin' — they're a
    # purpose-built integration credential.
    if api_keys.from_request(request) is not None:
        return "admin"
    if not auth.is_authenticated(request):
        return None
    payload = auth.session_payload(request)
    if not payload:
        return "admin"  # legacy session — pre-multi-user
    return payload.get("role") or "admin"


def has_role(actual: Optional[str], required: str) -> bool:
    if actual is None:
        return False
    if required not in ROLE_RANK:
        return False
    return ROLE_RANK.get(actual, 0) >= ROLE_RANK[required]
