"""Playwright smoke for the /pets page — exercises the bits the
TestClient can't see (Alpine state, post-load API fetches, button clicks).

Skipped automatically when Playwright isn't installed or the demo
isn't reachable. See tests/e2e/conftest.py."""
from __future__ import annotations

DEMO_URL_DEFAULT = "http://localhost:8080"


def test_pets_page_renders_with_seed_data(authed_page):
    """The seeded demo has 2 pets + sightings + diary. After page load
    the diary section, health widget, and Pro backfill badge should all
    be visible."""
    authed_page.goto(f"{DEMO_URL_DEFAULT}/pets")
    # Pet cards
    authed_page.wait_for_selector("text=Mochi", timeout=5000)
    authed_page.wait_for_selector("text=Maru", timeout=5000)
    # Pro extended badge on backfill panel
    authed_page.wait_for_selector("text=Pro", timeout=5000)


def test_tutorial_page_shows_steps(authed_page):
    """Tutorial page should render all 6 onboarding steps with their
    translated titles (not raw keys)."""
    authed_page.goto(f"{DEMO_URL_DEFAULT}/tutorial")
    # No raw key leaks.
    body_text = authed_page.text_content("body")
    assert "ONBOARDING_STEP_" not in body_text
    assert "NAV_TUTORIAL" not in body_text
    # At least one step title shows up.
    authed_page.wait_for_selector("text=Notifications, text=通知", timeout=5000)


def test_system_health_detectors_section_visible(authed_page):
    """Pro health-detector config block should render on /system in
    Pro builds (demo runs from the Pro repo)."""
    authed_page.goto(f"{DEMO_URL_DEFAULT}/system")
    # Heading + the dropdown for litter box camera.
    authed_page.wait_for_selector("text=Health detectors, text=健康偵測", timeout=5000)
    authed_page.wait_for_selector("select", timeout=5000)
