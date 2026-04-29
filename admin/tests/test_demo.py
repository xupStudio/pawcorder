"""Smoke tests for app.demo — the laptop-friendly mock-everything entry point.

The demo module has heavy import-time side effects (creates a temp dir,
patches docker_ops / camera_api / camera_setup / cloud, writes a fake
.env). These tests verify the most load-bearing patch — the brand-aware
dispatcher — actually lands on `camera_setup` so a click on Test/Save in
the demo UI never reaches a real httpx call.
"""
from __future__ import annotations

import importlib
import sys

import pytest


@pytest.mark.asyncio
async def test_demo_module_imports_and_stubs_camera_setup_dispatcher():
    """After `app.demo` is imported, calling
    `camera_setup.auto_configure_for_brand` returns the demo's mock dict
    (not a real httpx call), and the bound function is the demo mock.
    """
    # Purge any cached app.* modules so demo's import-time side effects
    # don't collide with whatever the previous test set up.
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)

    demo = importlib.import_module("app.demo")
    from app import camera_setup

    assert camera_setup.auto_configure_for_brand is demo._mock_auto_configure_for_brand

    result = await camera_setup.auto_configure_for_brand(
        "hikvision", "1.2.3.4", "x", "demo",
    )
    # Demo's mock dict shape — synthesised RTSP URLs + a device block,
    # never a real httpx exchange.
    assert result["device"]["model"] == "E1 Outdoor PoE"
    assert result["rtsp_main"].startswith("rtsp://x:demo@1.2.3.4")
    assert "rtsp_sub" in result
