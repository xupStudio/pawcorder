"""Top-level coordinator for the wireless-onboarding flow.

The orchestrator stitches the per-transport scanners and provisioners
together into a single API the admin web layer talks to over Server-
Sent Events. It handles:

  * Discovery: parallel BLE + SoftAP scans, deduped, sorted by RSSI.
  * Provisioner routing: pick the right ``BaseProvisioner`` subclass
    based on the discovered device's fingerprint + capability.
  * Vendor app handoff: detection-only devices still emit a clean
    ``ProvisionerResult`` so the UI can render the right deep link.
  * Arrival watcher: every ``ProvisionerResult`` with
    ``needs_arrival_watcher=True`` registers its MAC and the
    orchestrator emits an ``arrived`` event when the camera shows up.
  * Camera handoff: on arrival, call ``camera_setup.auto_configure_for_brand``
    so the existing IP-camera onboarding path takes over.

We expose two entry points:

  * ``discover()`` returns a one-shot list of devices currently in
    pairing mode. Used by ``GET /api/onboarding/wireless/scan``.
  * ``provision_stream()`` is an async generator the SSE route consumes.
    Each yielded dict is the SSE payload (``event``, ``data``).

Provisioner registration is *static* — every supported provisioner is
imported here so ``handles()`` checks are fast and we get import errors
at boot rather than mid-onboarding if a provisioner module is broken.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import AsyncIterator

from .. import camera_setup, camera_compat, onboarding as onboarding_mod
from .arrival_watcher import ArrivalWatcher
from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
    Transport,
)
from .ble_homekit import HomeKitProvisioner
from .ble_matter import MatterProvisioner
from .ble_proprietary import ProprietaryVendorProvisioner
from .esptouch_v2 import EspTouchProvisioner
from .qr_generic import GenericWifiQRProvisioner
from .qr_reolink import ReolinkQRProvisioner
from .softap_dahua import DahuaSoftAPProvisioner
from .softap_espressif import EspressifSoftAPProvisioner, ImouSoftAPProvisioner
from .softap_foscam import FoscamSoftAPProvisioner
from .softap_hnap import HNAPSoftAPProvisioner
from .wps_pbc import WPSProvisioner

logger = logging.getLogger("pawcorder.provisioning.orchestrator")


# Order matters: more specific subclasses come before catch-all ones
# so ``HomeKitProvisioner.handles(device)`` runs before
# ``ProprietaryVendorProvisioner.handles(device)``.
_PROVISIONERS: tuple[type[BaseProvisioner], ...] = (
    HomeKitProvisioner,
    MatterProvisioner,
    FoscamSoftAPProvisioner,
    DahuaSoftAPProvisioner,
    HNAPSoftAPProvisioner,
    EspressifSoftAPProvisioner,
    ImouSoftAPProvisioner,
    ReolinkQRProvisioner,
    GenericWifiQRProvisioner,
    EspTouchProvisioner,
    WPSProvisioner,
    # Catch-all for vendor-app handoff: anything left over with
    # capability == "vendor" lands here. Must be last in the list.
    ProprietaryVendorProvisioner,
)


def pick_provisioner(device: DiscoveredDevice) -> BaseProvisioner | None:
    for cls in _PROVISIONERS:
        if cls.handles(device):
            return cls()
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def discover(
    *, ble_duration_s: float = 6.0, do_ble: bool = True, do_softap: bool = True,
) -> list[DiscoveredDevice]:
    """Run all enabled scanners in parallel; return deduped list.

    Sorted strongest-signal-first so the UI's first card is the camera
    closest to the user — the right default for the "I just plugged it
    in" UX.
    """
    tasks: list[asyncio.Task] = []
    if do_ble:
        from . import ble_scanner  # lazy import keeps bleak optional
        tasks.append(asyncio.create_task(ble_scanner.scan_once(duration_seconds=ble_duration_s)))
    if do_softap:
        from . import softap_scanner
        tasks.append(asyncio.create_task(softap_scanner.scan_once()))
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)

    devices: dict[str, DiscoveredDevice] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.warning("scanner raised: %s", result)
            continue
        for d in result:
            existing = devices.get(d.id)
            if existing is None or d.signal_dbm > existing.signal_dbm:
                devices[d.id] = d

    return sorted(devices.values(), key=lambda d: d.signal_dbm, reverse=True)


def manual_qr_device(*, vendor_kind: str = "generic") -> DiscoveredDevice:
    """User picks "I have a QR-only camera" — synthesise a device record.

    QR-receive cameras don't broadcast on BLE or SoftAP — they're idle
    until they see a QR code in front of the lens. The UI offers a
    "Generate QR" button that calls into the orchestrator with this
    synthetic device so the rest of the pipeline can run unmodified.
    """
    if vendor_kind == "reolink":
        return DiscoveredDevice(
            id="manual-qr-reolink",
            transport="qr",
            vendor="reolink",
            label="Reolink (QR setup)",
            capability="qr",
            fingerprint_id="reolink-qr",
        )
    return DiscoveredDevice(
        id="manual-qr-generic",
        transport="qr",
        vendor="other",
        label="QR-receive camera",
        capability="qr",
        fingerprint_id="generic-qr",
    )


def manual_broadcast_device(*, kind: str = "esptouch") -> DiscoveredDevice:
    """User picks "I have an ESP32 / WPS camera" — synthesise a device record."""
    if kind == "wps":
        return DiscoveredDevice(
            id="manual-wps",
            transport="wps",
            vendor="other",
            label="WPS push-button camera",
            capability="broadcast",
            fingerprint_id="",
        )
    return DiscoveredDevice(
        id="manual-esptouch",
        transport="esptouch",
        vendor="other",
        label="ESP32 / EspTouch camera",
        capability="broadcast",
        fingerprint_id="",
    )


# ---------------------------------------------------------------------------
# SSE flow
# ---------------------------------------------------------------------------


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": data}


async def provision_stream(
    *,
    device: DiscoveredDevice,
    ssid: str,
    psk: str,
    auth: str = "wpa2-psk",
    arrival_timeout_s: float = 180.0,
) -> AsyncIterator[dict]:
    """Drive provisioning + arrival watch + auto-configure as SSE events.

    Yields::

        {"event": "selected", "data": {"device": {...}}}
        {"event": "provisioning", "data": {"transport": "softap", ...}}
        {"event": "result", "data": {"ok": True, ...}}
        {"event": "waiting_for_arrival", "data": {"mac": "..."}}
        {"event": "arrived", "data": {"ip": "192.168.x.y"}}
        {"event": "configured", "data": {"camera": {...}}}
        {"event": "done", "data": {}}

    Failure paths emit ``error`` events with a ``message`` field; the
    SSE consumer renders that string verbatim.
    """
    yield _sse("selected", device=device.to_dict())
    provisioner = pick_provisioner(device)
    if provisioner is None:
        yield _sse(
            "error",
            message=(
                f"No provisioner matched this {device.vendor} camera. "
                "Set it up with the vendor's app and Pawcorder will "
                "detect it once it's on Wi-Fi."
            ),
        )
        return

    yield _sse("provisioning", transport=provisioner.transport)
    try:
        result = await provisioner.provision(
            ProvisioningRequest(device=device, ssid=ssid, psk=psk, auth=auth)
        )
    except Exception as exc:  # noqa: BLE001 - surface protocol errors verbatim
        logger.exception("provisioner crashed")
        yield _sse("error", message=f"Provisioning crashed: {exc}")
        return
    yield _sse("result", **result.to_dict())

    if not result.ok:
        return
    if not result.needs_arrival_watcher:
        yield _sse("done")
        return

    watcher = ArrivalWatcher()
    if result.mac:
        watcher.expect(result.mac)
    if device.mac:
        watcher.expect(device.mac)

    yield _sse("waiting_for_arrival", macs=watcher.expected_macs, timeout_s=arrival_timeout_s)
    arrival_ip = ""
    async for arrival in watcher.stream(timeout_s=arrival_timeout_s):
        arrival_ip = arrival.ip
        yield _sse("arrived", ip=arrival.ip, mac=arrival.mac)
        break

    if not arrival_ip:
        yield _sse(
            "timed_out",
            message=(
                "The camera did not appear on the network within the "
                "expected window. Double-check the Wi-Fi password and "
                "try again, or finish setup with the vendor's app."
            ),
        )
        return

    # Hand off to the existing camera_setup pipeline so RTSP is enabled,
    # default credentials are tried, and the brand's API surface is
    # walked. We use the brand the fingerprint already gave us; for
    # detect-only / unknown vendors the dispatcher falls back to ONVIF.
    brand = device.vendor or "other"
    if brand not in camera_compat.BRANDS:
        brand = "other"
    try:
        config = await camera_setup.auto_configure_for_brand(
            brand=brand, ip=arrival_ip, user="admin", password="",
        )
    except PermissionError:
        # Default creds didn't work — surface a friendly message but
        # still report the IP so the user can finish add-camera flow.
        yield _sse(
            "configured_partial",
            ip=arrival_ip,
            brand=brand,
            message=(
                "The camera joined the network but Pawcorder couldn't "
                "log in with the default password. Finish adding it on "
                "the Cameras page with your camera's password."
            ),
        )
        yield _sse("done")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("camera_setup post-arrival call failed")
        yield _sse("error", message=f"Camera joined but configuration failed: {exc}")
        return

    yield _sse(
        "configured",
        ip=arrival_ip,
        brand=brand,
        result=config,
    )
    # Drop the dashboard-onboarding marker so the "add a wireless camera"
    # step in the onboarding widget ticks off — the user has now used
    # the feature end-to-end.
    onboarding_mod.mark_wireless_onboarded()
    yield _sse("done")
