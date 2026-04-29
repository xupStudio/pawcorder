"""Tests for the brand-aware dispatcher in camera_setup.

These tests stub the per-vendor `auto_configure` functions on each brand
module so we don't make real network calls. We're verifying the dispatcher's
routing logic, not the per-vendor protocols (those have their own tests).
"""
from __future__ import annotations

import pytest

from app import camera_setup


@pytest.mark.parametrize("brand,expected", [
    ("tapo",    True),
    ("imou",    True),
    ("wyze",    True),
    ("other",   False),  # falls through to ONVIF discovery; UI still shows guidance
    ("reolink", False),
    ("hikvision", False),
    ("dahua",   False),
    ("amcrest", False),
    ("axis",    False),
    ("foscam",  False),
    ("ubiquiti", True),  # UniFi Protect — controller-flow not shipped yet, manual for now
    ("nonexistent-brand", False),  # unknown → not manual; falls back to ONVIF
])
def test_is_manual_brand_matches_camera_compat(brand: str, expected: bool):
    assert camera_setup.is_manual_brand(brand) is expected


@pytest.mark.asyncio
async def test_manual_brand_returns_sentinel_without_calling_any_handler(monkeypatch):
    called: list[str] = []

    async def _spy(*args, **kwargs):
        called.append("HANDLER_CALLED")
        return {}

    # Make sure no handler is invoked for a manual brand even if its module
    # would have been mapped.
    monkeypatch.setitem(camera_setup._BRAND_MODULES, "tapo", _spy)

    result = await camera_setup.auto_configure_for_brand("tapo", "1.2.3.4", "u", "p")
    assert result == {
        "device": None, "link": None, "connection_type": "unknown",
        "rtsp_main": "", "rtsp_sub": "", "manual": True,
    }
    assert called == [], "manual brand must short-circuit before the dispatch table"


@pytest.mark.asyncio
async def test_known_brand_routes_to_module_handler(monkeypatch):
    async def _hikvision_stub(ip, user, password):
        assert ip == "192.168.1.50"
        assert user == "admin"
        assert password == "secret"
        return {"device": {"manufacturer": "Hikvision"}, "rtsp_main": "rtsp://h", "rtsp_sub": ""}

    # Patch on the exact module the dispatcher holds (defensive against any
    # import-path quirks pytest might have between test files).
    monkeypatch.setattr(camera_setup._BRAND_MODULES["hikvision"], "auto_configure", _hikvision_stub)

    result = await camera_setup.auto_configure_for_brand(
        "hikvision", "192.168.1.50", "admin", "secret",
    )
    assert result["device"]["manufacturer"] == "Hikvision"
    assert result["rtsp_main"] == "rtsp://h"


@pytest.mark.asyncio
async def test_amcrest_and_dahua_both_route_to_dahua_module(monkeypatch):
    # Amcrest is a Dahua OEM; both brand keys should invoke dahua_api.auto_configure.
    called: list[str] = []

    async def _dahua_stub(ip, user, password):
        called.append(ip)
        return {"device": {"manufacturer": "Dahua"}, "connection_type": "wired",
                "link": None, "rtsp_main": "", "rtsp_sub": ""}

    monkeypatch.setattr(camera_setup._BRAND_MODULES["dahua"], "auto_configure", _dahua_stub)

    await camera_setup.auto_configure_for_brand("amcrest", "1.2.3.4", "u", "p")
    await camera_setup.auto_configure_for_brand("dahua", "5.6.7.8", "u", "p")
    assert called == ["1.2.3.4", "5.6.7.8"]


@pytest.mark.asyncio
async def test_unknown_brand_falls_back_to_onvif(monkeypatch):
    called: list[tuple] = []

    async def _onvif_stub(ip, user, password):
        called.append((ip, user, password))
        return {"device": {"manufacturer": "ONVIF"}, "rtsp_main": "rtsp://o", "rtsp_sub": ""}

    # camera_setup imports onvif_helper at module level; patch via the direct attribute.
    monkeypatch.setattr(camera_setup.onvif_helper, "auto_configure", _onvif_stub)

    result = await camera_setup.auto_configure_for_brand(
        "totally-made-up", "10.0.0.1", "root", "pw",
    )
    assert called == [("10.0.0.1", "root", "pw")]
    assert result["device"]["manufacturer"] == "ONVIF"


@pytest.mark.asyncio
async def test_handler_exceptions_propagate(monkeypatch):
    async def _failing(*_a, **_kw):
        raise PermissionError("invalid credentials")

    monkeypatch.setattr(camera_setup._BRAND_MODULES["axis"], "auto_configure", _failing)

    with pytest.raises(PermissionError, match="invalid credentials"):
        await camera_setup.auto_configure_for_brand("axis", "1.2.3.4", "root", "wrong")
