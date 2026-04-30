"""Shared fixtures for Playwright E2E.

These tests are opt-in. They expect a running demo on localhost:8080
(`make demo`). To run:

    cd admin
    .venv/bin/pip install playwright
    .venv/bin/playwright install chromium
    .venv/bin/python -m pytest tests/e2e/

Excluded from the main run by tests/e2e/conftest.py setting a
`pytest.mark.skip` on collection if Playwright isn't installed, so a
fresh `pytest tests/` doesn't bomb on missing deps.
"""
from __future__ import annotations

import os
import urllib.request

import pytest

DEMO_URL = os.environ.get("PAWCORDER_E2E_URL", "http://localhost:8080")
DEMO_PASSWORD = os.environ.get("PAWCORDER_E2E_PASSWORD", "demo")


def _demo_running() -> bool:
    try:
        with urllib.request.urlopen(f"{DEMO_URL}/login", timeout=2):
            return True
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip the whole e2e dir if Playwright isn't installed or the
    demo isn't reachable. Keeps `pytest tests/` green for users who
    haven't opted in."""
    skip_pw = pytest.mark.skip(reason="playwright not installed")
    skip_demo = pytest.mark.skip(reason=f"demo not reachable at {DEMO_URL}")
    pw = _playwright_available()
    demo = _demo_running() if pw else False
    for item in items:
        if "tests/e2e" not in str(item.fspath):
            continue
        if not pw:
            item.add_marker(skip_pw)
        elif not demo:
            item.add_marker(skip_demo)


@pytest.fixture(scope="session")
def browser_ctx():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        yield ctx
        ctx.close()
        browser.close()


@pytest.fixture
def page(browser_ctx):
    pg = browser_ctx.new_page()
    yield pg
    pg.close()


@pytest.fixture
def authed_page(page):
    """Logged-in page ready for further navigation."""
    page.goto(f"{DEMO_URL}/login")
    page.fill("input[type='password']", DEMO_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_url(f"{DEMO_URL}/")
    return page
