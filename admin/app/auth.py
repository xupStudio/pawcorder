"""Single-password admin auth using a signed session cookie.

The shared secret and expected password come from the .env that the admin
panel itself manages. We re-read .env on each check so password rotation
takes effect without restarting the container.

CSRF defence: the session cookie is `samesite=lax`, which already blocks
the form-encoded cross-site POST attack vector. As a belt-and-braces
second factor, every mutating endpoint (POST/PUT/DELETE) also requires a
custom `X-Requested-With: pawcorder` header. Browsers refuse to send
custom headers on cross-origin requests without a successful CORS
preflight, which we never grant — so a malicious page can't forge one.
The frontend's `api()` helper attaches the header automatically.
"""
from __future__ import annotations

import hmac
import secrets
from typing import Optional

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config_store

COOKIE_NAME = "pawcorder_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 days
CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "pawcorder"
# Mutating verbs that must carry the CSRF header. GET/HEAD/OPTIONS pass.
CSRF_GUARDED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _serializer() -> URLSafeTimedSerializer:
    cfg = config_store.load_config()
    secret = cfg.admin_session_secret or "pawcorder-bootstrap"
    return URLSafeTimedSerializer(secret, salt="pawcorder-session-v1")


def issue_session(*, username: Optional[str] = None,
                   role: Optional[str] = None) -> str:
    """Mint a session token. Pre-multi-user callers omit username/role
    and the session is treated as legacy admin (full access)."""
    payload: dict = {"v": 1, "n": secrets.token_hex(8)}
    if username is not None:
        payload["u"] = username
        payload["r"] = role or "admin"
    return _serializer().dumps(payload)


def verify_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return True
    except (BadSignature, SignatureExpired):
        return False


def session_payload(request: Request) -> Optional[dict]:
    """Decoded session cookie payload, or None for unauthenticated /
    invalid. Used by users.role_from_request to pull the role."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "username": data.get("u"),
        "role": data.get("r"),
    }


def password_matches(submitted: str) -> bool:
    """Used ONLY by the legacy single-password path. Multi-user installs
    go through users.authenticate() which checks per-user hashes."""
    cfg = config_store.load_config()
    expected = cfg.admin_password or ""
    if not expected:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    return verify_session(request.cookies.get(COOKIE_NAME))


def has_csrf_header(request: Request) -> bool:
    """Confirm the X-Requested-With header is present and equal to our
    sentinel value. Returns True for non-mutating methods (no check
    required there) so route code can use a single guard."""
    if request.method.upper() not in CSRF_GUARDED_METHODS:
        return True
    got = request.headers.get(CSRF_HEADER) or request.headers.get(CSRF_HEADER.lower())
    return got == CSRF_HEADER_VALUE
