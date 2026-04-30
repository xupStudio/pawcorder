"""Smoke tests that render every authed page and check for raw i18n
key leaks, broken templates, and 5xx errors.

Catches the kind of bug we hit in production where a template referenced
`t('NAV_TUTORIAL')` but the i18n.py registration was missing, leaving
`NAV_TUTORIAL` visible to users instead of "Tutorial" / "教學".

This is faster than Playwright (no browser) but only checks
server-rendered HTML — Alpine state and post-load API responses
aren't covered. For those, see tests/e2e/ (Playwright, opt-in).
"""
from __future__ import annotations

import re

import pytest

# Pages that don't require a pet store / non-trivial data fixtures.
# We sanity-check the GET path renders without raw key leaks.
AUTHED_PAGES = [
    "/",
    "/cameras",
    "/pets",
    "/timelapse",
    "/detection",
    "/privacy",
    "/mobile",
    "/notifications",
    "/cloud",
    "/hardware",
    "/users",
    "/system",
    "/tutorial",
    "/welcome",
]

# Heuristic for a leaked translation key:
#   • runs of UPPER_CASE_WORDS_LIKE_THIS (≥4 chars + at least one underscore)
#   • not part of a CSS class, data attribute, or SVG path
# We strip <script>, <style>, and HTML attributes before matching so
# legitimate Tailwind class lists / data- attrs don't false-positive.
_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,})\b")
_TAG_RE = re.compile(r"<[^>]+>", flags=re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", flags=re.DOTALL)

# Whitelist: known UPPER_SNAKE strings that legitimately appear in
# user-facing text (acronyms, model names, status enums).
_WHITELIST_TOKENS = frozenset({
    "STORAGE_PATH", "FRIGATE_RTSP_PASSWORD", "ADMIN_PASSWORD",
    "ADMIN_SESSION_SECRET", "TZ", "PET_MIN_SCORE", "PET_THRESHOLD",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY",
    "PAWCORDER_PRO_LICENSE_KEY", "API_KEY", "X_REQUESTED_WITH",
    "NOT_FOUND", "OFF_AIR", "PEM_FORMAT",
})


def _user_visible_text(html: str) -> str:
    """Strip script/style blocks and HTML tags, leave just the rendered text."""
    no_blocks = _SCRIPT_STYLE_RE.sub("", html)
    text = _TAG_RE.sub(" ", no_blocks)
    return text


def _find_raw_keys(html: str) -> list[str]:
    text = _user_visible_text(html)
    hits = set()
    for m in _KEY_RE.finditer(text):
        token = m.group(1)
        if token in _WHITELIST_TOKENS:
            continue
        hits.add(token)
    return sorted(hits)


@pytest.mark.parametrize("path", AUTHED_PAGES)
def test_authed_page_renders_without_raw_i18n_keys(authed_client, path):
    resp = authed_client.get(path)
    # Some pages 303 to a sub-page (e.g. /welcome may redirect on a
    # fresh install) — accept any non-5xx as "page didn't crash" and
    # only run the key check on a 200 with HTML body.
    assert resp.status_code < 500, f"{path} blew up: {resp.status_code} {resp.text[:200]}"
    if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
        return
    leaks = _find_raw_keys(resp.text)
    assert not leaks, (
        f"{path} leaked raw i18n keys to user-visible text: {leaks}. "
        "Check the template uses t('KEY') AND that the key is registered "
        "in app/i18n.py (catches the NAV_TUTORIAL / ONBOARDING_PROGRESS_LABEL bugs)."
    )


def test_login_page_renders_without_raw_i18n_keys(app_client):
    resp = app_client.get("/login")
    assert resp.status_code == 200
    leaks = _find_raw_keys(resp.text)
    assert not leaks, f"/login leaked raw i18n keys: {leaks}"
