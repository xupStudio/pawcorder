"""Tests for top-level helpers in app.main.

These exercise small pure-ish helpers (no FastAPI routes — the routes are
covered by test_routes.py via TestClient). The fixtures in conftest set
up PAWCORDER_DATA_DIR + stubs so importing app.main is safe.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_best_effort_connection_type_short_circuits_for_manual_brand(
    data_dir, monkeypatch
):
    """Manual-setup brands (tapo / imou / wyze) must short-circuit to
    "unknown" without round-tripping through the brand dispatcher.

    We replace `camera_setup.auto_configure_for_brand` with a function that
    raises so the test fails loudly if the helper ever drops into it.
    """
    from app import camera_setup, main as main_module
    from app.cameras_store import Camera

    async def _explode(*_args, **_kwargs):
        raise AssertionError("dispatcher must NOT be called for manual brands")

    monkeypatch.setattr(camera_setup, "auto_configure_for_brand", _explode)

    cam = Camera(name="kitchen", ip="192.168.1.10", password="x", brand="tapo")
    result = await main_module._best_effort_connection_type(cam)
    assert result == "unknown"
