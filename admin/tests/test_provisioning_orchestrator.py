"""Tests for the wireless-onboarding orchestrator.

The orchestrator wires per-transport scanners + provisioners + the
arrival watcher together; we test routing, vendor handoff, and the
SSE event stream against mock provisioners so we don't need real
hardware.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


def _make_device(**overrides):
    from app.provisioning.base import DiscoveredDevice

    base = dict(
        id="x", transport="ble", vendor="other", model="", label="",
        mac="aa:bb:cc:dd:ee:ff", ssid="", signal_dbm=-50,
        capability="auto", fingerprint_id="",
    )
    base.update(overrides)
    return DiscoveredDevice(**base)


def test_pick_provisioner_routes_homekit(data_dir):
    from app.provisioning import orchestrator
    from app.provisioning.ble_homekit import HomeKitProvisioner

    d = _make_device(fingerprint_id="homekit-generic")
    p = orchestrator.pick_provisioner(d)
    assert isinstance(p, HomeKitProvisioner)


def test_pick_provisioner_routes_foscam_softap(data_dir):
    from app.provisioning import orchestrator
    from app.provisioning.softap_foscam import FoscamSoftAPProvisioner

    d = _make_device(transport="softap", fingerprint_id="foscam-softap")
    p = orchestrator.pick_provisioner(d)
    assert isinstance(p, FoscamSoftAPProvisioner)


def test_pick_provisioner_routes_proprietary_to_vendor_handoff(data_dir):
    from app.provisioning import orchestrator
    from app.provisioning.ble_proprietary import ProprietaryVendorProvisioner

    d = _make_device(capability="vendor", fingerprint_id="tapo-ble")
    p = orchestrator.pick_provisioner(d)
    assert isinstance(p, ProprietaryVendorProvisioner)


def test_pick_provisioner_returns_none_for_unknown_device(data_dir):
    from app.provisioning import orchestrator

    d = _make_device(transport="ble", capability="auto", fingerprint_id="")
    assert orchestrator.pick_provisioner(d) is None


def test_manual_qr_device_factory(data_dir):
    from app.provisioning import orchestrator

    d = orchestrator.manual_qr_device(vendor_kind="reolink")
    assert d.fingerprint_id == "reolink-qr"
    d2 = orchestrator.manual_qr_device()
    assert d2.fingerprint_id == "generic-qr"


def test_provision_stream_qr_emits_image(monkeypatch, data_dir):
    from app.provisioning import orchestrator
    from app.provisioning.base import DiscoveredDevice

    async def run():
        device = orchestrator.manual_qr_device()
        events = []
        async for evt in orchestrator.provision_stream(
            device=device, ssid="Home", psk="pw", auth="wpa2-psk",
            arrival_timeout_s=0.05,
        ):
            events.append(evt)
            # Stop after the QR result arrives — we don't need the full
            # arrival-watcher dance for this test.
            if evt["event"] == "result":
                break
        return events

    events = asyncio.run(run())
    kinds = [e["event"] for e in events]
    assert "selected" in kinds
    assert "provisioning" in kinds
    assert "result" in kinds
    result_event = next(e for e in events if e["event"] == "result")
    assert "WIFI:S:Home" in result_event["data"]["image_payload"]


def test_provision_stream_vendor_handoff_emits_correct_message(data_dir):
    from app.provisioning import orchestrator
    from app.provisioning.base import DiscoveredDevice

    async def run():
        device = DiscoveredDevice(
            id="tapo-1", transport="ble", vendor="tapo",
            label="Tapo C200", mac="9c:53:22:00:00:01",
            capability="vendor", fingerprint_id="tapo-ble",
        )
        events = []
        async for evt in orchestrator.provision_stream(
            device=device, ssid="Home", psk="pw", auth="wpa2-psk",
            arrival_timeout_s=0.05,
        ):
            events.append(evt)
            if evt["event"] == "result":
                break
        return events

    events = asyncio.run(run())
    result = next(e for e in events if e["event"] == "result")
    assert result["data"]["transport"] == "vendor_app"
    assert "Tapo" in result["data"]["message"] or "tapo" in result["data"]["message"].lower()


def test_provision_stream_emits_error_on_unknown_device(data_dir):
    from app.provisioning import orchestrator

    async def run():
        device = _make_device(
            transport="ble", capability="auto", fingerprint_id="",
        )
        events = []
        async for evt in orchestrator.provision_stream(
            device=device, ssid="Home", psk="pw",
        ):
            events.append(evt)
        return events

    events = asyncio.run(run())
    error = next((e for e in events if e["event"] == "error"), None)
    assert error is not None
    assert "No provisioner matched" in error["data"]["message"]


def test_discover_runs_both_scanners(monkeypatch, data_dir):
    from app.provisioning import ble_scanner, softap_scanner, orchestrator
    from app.provisioning.base import DiscoveredDevice

    async def fake_ble(*, duration_seconds: float = 6.0):
        return [DiscoveredDevice(
            id="ble-1", transport="ble", vendor="other",
            mac="aa:00:00:00:00:01", signal_dbm=-40,
            capability="auto", fingerprint_id="matter-generic",
        )]

    async def fake_softap():
        return [DiscoveredDevice(
            id="softap-1", transport="softap", vendor="foscam",
            mac="bb:00:00:00:00:01", signal_dbm=-60,
            capability="auto", fingerprint_id="foscam-softap",
        )]

    monkeypatch.setattr(ble_scanner, "scan_once", fake_ble)
    monkeypatch.setattr(softap_scanner, "scan_once", fake_softap)

    devices = asyncio.run(orchestrator.discover())
    ids = {d.id for d in devices}
    assert "ble-1" in ids
    assert "softap-1" in ids
    # Stronger-signal first.
    assert devices[0].signal_dbm >= devices[-1].signal_dbm
