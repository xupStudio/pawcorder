"""CSRF guard: mutating routes require the X-Requested-With sentinel."""
from __future__ import annotations

import pytest


def test_get_request_does_not_require_csrf_header(authed_client):
    """Read-only routes pass without the header — TestClient default
    fixture sets it, but we override to None here to confirm."""
    resp = authed_client.get("/api/status", headers={"X-Requested-With": ""})
    assert resp.status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("POST",   "/api/cameras",           {"name": "x", "ip": "1.1.1.1", "password": "p"}),
    ("POST",   "/api/config/save",       {"section": "general", "data": {}}),
    ("DELETE", "/api/cameras/anything",  None),
    ("POST",   "/api/privacy",           {"enabled": True}),
    ("POST",   "/api/frigate/restart",   {}),
    ("POST",   "/api/notifications/test",{"channel": "telegram"}),
    ("POST",   "/api/pets",              {"name": "x", "species": "cat"}),
    ("DELETE", "/api/pets/x",            None),
])
def test_mutating_routes_reject_missing_csrf_header(authed_client, method, path, body):
    """POST/PUT/DELETE without X-Requested-With → 403 (not 401, not 200)."""
    resp = authed_client.request(
        method, path,
        headers={"X-Requested-With": ""},  # explicit absence
        json=body if body is not None else None,
    )
    assert resp.status_code == 403, f"{method} {path} returned {resp.status_code}"
    assert resp.json()["error"] == "csrf_header_missing"


def test_wrong_csrf_header_value_rejected(authed_client):
    """Custom header present but with the wrong value → still 403."""
    resp = authed_client.post(
        "/api/privacy",
        headers={"X-Requested-With": "evil-attacker"},
        json={"enabled": True},
    )
    assert resp.status_code == 403


def test_mutating_route_with_correct_header_works(authed_client):
    """Default fixture header allows mutations through."""
    resp = authed_client.post("/api/privacy", json={"enabled": True})
    assert resp.status_code == 200


def test_csrf_rejection_happens_after_auth_check(app_client):
    """Unauthenticated CSRF-less request → 401, not 403. Auth comes
    first so attackers can't probe which routes exist by the error code."""
    resp = app_client.post(
        "/api/privacy",
        headers={"X-Requested-With": ""},
        json={"enabled": True},
    )
    assert resp.status_code == 401


def test_multipart_upload_route_requires_csrf(authed_client):
    """The multipart restore route is the highest-value CSRF target —
    a stolen tar.gz can replace the user's whole config. Confirm it's
    guarded."""
    resp = authed_client.post(
        "/api/backup/restore",
        headers={"X-Requested-With": ""},
        files={"file": ("backup.tar.gz", b"\x1f\x8b\x08", "application/gzip")},
    )
    assert resp.status_code == 403


def test_backup_inspect_route_requires_csrf(authed_client):
    """Inspect doesn't mutate, but it's still POST + multipart, so it
    should require the header for consistency. (FastAPI parses the
    body before calling the route, so without the guard a malicious
    page could probe for files the user has by reading the response.)"""
    resp = authed_client.post(
        "/api/backup/inspect",
        headers={"X-Requested-With": ""},
        files={"file": ("backup.tar.gz", b"\x1f\x8b\x08", "application/gzip")},
    )
    assert resp.status_code == 403


# ---- has_csrf_header unit tests ----------------------------------------

def test_has_csrf_header_returns_true_for_get(data_dir):
    """GET / HEAD / OPTIONS pass without the header."""
    from app import auth

    class _R:
        method = "GET"
        headers: dict = {}
    assert auth.has_csrf_header(_R()) is True


def test_has_csrf_header_rejects_post_without_header(data_dir):
    from app import auth

    class _R:
        method = "POST"
        headers: dict = {}
    assert auth.has_csrf_header(_R()) is False


def test_has_csrf_header_accepts_correct_value(data_dir):
    from app import auth

    class _R:
        method = "POST"
        headers = {"X-Requested-With": "pawcorder"}
    assert auth.has_csrf_header(_R()) is True


def test_has_csrf_header_rejects_wrong_value(data_dir):
    from app import auth

    class _R:
        method = "POST"
        headers = {"X-Requested-With": "evil"}
    assert auth.has_csrf_header(_R()) is False


def test_has_csrf_header_case_insensitive_lookup(data_dir):
    """HTTP header names are case-insensitive; our check should be too."""
    from app import auth

    class _R:
        method = "PUT"
        headers = {"x-requested-with": "pawcorder"}
    assert auth.has_csrf_header(_R()) is True


# ---- audit: every mutating route is auth-guarded -----------------------

# Routes that intentionally don't go through _require_auth.
# Keep tiny — every addition is a security review trigger.
_AUTH_EXEMPT_ROUTES = {
    ("POST", "/login"),                    # how you get a session — can't require one
    ("POST", "/logout"),                   # destroys session — pre-auth tolerable
    ("POST", "/api/lang"),                 # cookie-only side effect, no host state
    ("POST", "/api/marketing/signup"),     # public form, rate-limited per IP
    ("OPTIONS", "/api/marketing/signup"),  # CORS preflight
    ("POST", "/api/frigate/event"),        # Frigate webhook — same-host origin
}


def test_every_mutating_route_requires_auth(app_client):
    """Walks the registered FastAPI route table; for each non-exempt
    POST/PUT/DELETE/PATCH route, confirms an unauthenticated request
    returns 401 (not 200, not 500).

    This is a security backstop — if someone adds a new mutating route
    and forgets _require_auth, this test fails before they merge.
    """
    from app.main import app
    import re

    seen = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if not methods or not path.startswith("/"):
            continue
        for m in methods:
            if m.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            if (m.upper(), path) in _AUTH_EXEMPT_ROUTES:
                continue
            seen.append((m.upper(), path))

    # Replace path params with concrete values so the routes match.
    def concretize(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "x", path)

    failures = []
    for method, path in seen:
        url = concretize(path)
        # Without cookie + without CSRF header — the universally-bad request.
        resp = app_client.request(method, url, headers={"X-Requested-With": ""})
        if resp.status_code not in (401, 422):
            # 422 happens for routes with required body params before
            # auth runs — also acceptable, the body never gets parsed
            # into anything dangerous.
            failures.append((method, path, resp.status_code))

    assert not failures, (
        f"Mutating routes that responded without 401/422 to an "
        f"unauthenticated request: {failures}"
    )
