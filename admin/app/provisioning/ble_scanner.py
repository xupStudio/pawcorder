"""BLE-based discovery for cameras in pairing mode.

We use ``bleak`` (MIT) to listen for BLE advertisements, run each one
through ``fingerprints.match_ble``, and emit ``DiscoveredDevice`` records.

The scanner runs *passively* — we never connect to a device just to
fingerprint it. Connection only happens when the user opts a specific
device into provisioning, which keeps RF behaviour predictable on hosts
with iffy Bluetooth stacks (notably Linux 5.x kernels with BlueZ < 5.66
where active scans can deadlock the controller).

Lazy imports: ``bleak`` is loaded inside the scan coroutine so an admin
that never opens the wireless-onboarding page never pays its import
cost. ``BLEAK_AVAILABLE`` exposes the probe to the orchestrator so it
can render a "no Bluetooth adapter found" hint instead of just silently
yielding zero results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import AsyncIterator

from .base import DiscoveredDevice, normalise_mac
from .fingerprints import Fingerprint, match_ble

logger = logging.getLogger("pawcorder.provisioning.ble")


# Path the host-side BLE scanner writes to. The container can't access
# CoreBluetooth on macOS Docker Desktop and can't reach the host's
# BlueZ socket on Linux, so install.sh installs a bleak-in-host-venv
# helper (scripts/ble-scan.sh) that drops a snapshot here every 30s.
# We read this file in preference to running bleak ourselves.
_HOST_BLE_FILE = os.environ.get("BLE_SCAN_FILE", "/data/.ble_scan.json")
# Same staleness bound as the wifi/arp helpers — three missed cycles
# before we give up and fall through to in-container bleak (which only
# works on bare-metal Linux installs).
_HOST_BLE_MAX_AGE = 90


def _host_ble_payload() -> dict | None:
    try:
        st = os.stat(_HOST_BLE_FILE)
    except (FileNotFoundError, PermissionError):
        return None
    if (time.time() - st.st_mtime) > _HOST_BLE_MAX_AGE:
        return None
    try:
        with open(_HOST_BLE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    return data


def _host_ble_devices() -> list[DiscoveredDevice]:
    """Return DiscoveredDevice records from the fresh host snapshot.

    We pass each advertisement through ``match_ble`` so a SoftAP-style
    fingerprint match still tags the device with vendor/label. Devices
    that don't match any fingerprint are dropped here — they're surfaced
    separately via the visible-ble API so the user can still see them
    in the troubleshooting disclosure.
    """
    payload = _host_ble_payload()
    if payload is None:
        return []
    out: list[DiscoveredDevice] = []
    for d in payload.get("devices") or []:
        if not isinstance(d, dict):
            continue
        addr = (d.get("address") or "").strip()
        local_name = (d.get("name") or "").strip()
        uuids = list(d.get("service_uuids") or [])
        mfg = [int(m) for m in (d.get("manufacturer_ids") or []) if isinstance(m, (int, str))]
        mac = normalise_mac(addr)
        fp = match_ble(
            advertised_uuids=uuids, local_name=local_name,
            mac=mac, manufacturer_ids=mfg,
        )
        if fp is None:
            continue
        try:
            rssi = int(d.get("rssi") or 0)
        except (TypeError, ValueError):
            rssi = 0
        out.append(DiscoveredDevice(
            id=mac or addr or f"{fp.id}-{local_name or 'anon'}",
            transport="ble",
            vendor=fp.vendor,
            model=fp.label,
            label=local_name or fp.label,
            mac=mac,
            signal_dbm=rssi,
            capability=fp.capability,
            fingerprint_id=fp.id,
            extra={
                "service_uuids": uuids,
                "manufacturer_ids": mfg,
                "vendor_app_ios": fp.metadata.get("vendor_app_ios", ""),
                "vendor_app_android": fp.metadata.get("vendor_app_android", ""),
            },
        ))
    return out


def bleak_available() -> bool:
    """True if BLE scanning is reachable through *some* path.

    We say yes if the host helper has a fresh snapshot OR the local
    bleak module can be imported (the bare-metal Linux fallback). The
    orchestrator hits this on every page load to decide whether to
    render the BLE column.
    """
    if _host_ble_payload() is not None:
        return True
    try:
        import bleak  # noqa: F401
    except ImportError:
        return False
    return True


async def scan_once(*, duration_seconds: float = 6.0) -> list[DiscoveredDevice]:
    """One-shot scan; returns a deduped list of fingerprinted devices.

    Prefers the host helper snapshot (always works, never blocked by
    Docker network isolation); falls through to in-container bleak
    when the helper isn't installed (Linux bare-metal). A 6-second
    window is the bleak default and matches what most BLE cameras
    advertise on (50–200 ms interval, so we catch ~30+ frames).
    """
    host = _host_ble_devices()
    if host or _host_ble_payload() is not None:
        # Either we got matches or the helper is fresh but found
        # nothing — both mean "host helper is the source of truth";
        # don't double up by also running an in-container scan that
        # we know fails on macOS / non-host-net Linux.
        return host
    devices: dict[str, DiscoveredDevice] = {}
    async for d in scan_stream(duration_seconds=duration_seconds):
        # Newer signal wins — RSSI updates as the user moves closer.
        devices[d.id] = d
    return list(devices.values())


async def scan_stream(
    *,
    duration_seconds: float = 6.0,
) -> AsyncIterator[DiscoveredDevice]:
    """Yield ``DiscoveredDevice`` records as they're seen.

    Used by the SSE orchestrator so the UI can update live as cameras
    appear, rather than waiting for the whole window to elapse.
    """
    try:
        # bleak >= 0.21 exposes BleakScanner.discover with a
        # detection_callback. We use the lower-level start/stop so we
        # can emit results progressively.
        from bleak import BleakScanner
    except ImportError as exc:
        logger.info("bleak not installed: %s", exc)
        return

    seen: dict[str, DiscoveredDevice] = {}
    queue: asyncio.Queue[DiscoveredDevice] = asyncio.Queue()

    def _on_advert(device, advertisement_data) -> None:  # bleak callback
        try:
            uuids = list(advertisement_data.service_uuids or ())
            local_name = (
                advertisement_data.local_name
                or getattr(device, "name", "")
                or ""
            )
            mfg = list((advertisement_data.manufacturer_data or {}).keys())
            mac = normalise_mac(getattr(device, "address", "") or "")
            rssi = int(getattr(advertisement_data, "rssi", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("ble advert parse failed: %s", exc)
            return

        fp = match_ble(
            advertised_uuids=uuids,
            local_name=local_name,
            mac=mac,
            manufacturer_ids=mfg,
        )
        if fp is None:
            return

        device_id = mac or f"{fp.id}-{local_name or 'anon'}"
        if device_id in seen:
            # Refresh the RSSI so the UI sorts correctly.
            existing = seen[device_id]
            if rssi and rssi != existing.signal_dbm:
                existing.signal_dbm = rssi
            return

        d = DiscoveredDevice(
            id=device_id,
            transport="ble",
            vendor=fp.vendor,
            model=fp.label,
            label=local_name or fp.label,
            mac=mac,
            signal_dbm=rssi,
            capability=fp.capability,
            fingerprint_id=fp.id,
            extra={
                "service_uuids": uuids,
                "manufacturer_ids": mfg,
                "vendor_app_ios": fp.metadata.get("vendor_app_ios", ""),
                "vendor_app_android": fp.metadata.get("vendor_app_android", ""),
            },
        )
        seen[device_id] = d
        queue.put_nowait(d)

    # bleak versions < 0.21 used a positional callback signature; >=
    # 0.21 keyword-only. Use the keyword form which works on all
    # currently-supported releases (the floor we set in requirements
    # is 0.21).
    scanner = BleakScanner(detection_callback=_on_advert)
    deadline = time.monotonic() + max(0.5, duration_seconds)
    try:
        await scanner.start()
    except Exception as exc:  # noqa: BLE001
        # Common failure: no BLE adapter on the host, or BlueZ rejected
        # the start (D-Bus permission, USB dongle missing). Surface a
        # one-line warning rather than crash; the orchestrator will see
        # an empty stream and tell the user.
        logger.warning("ble scan start failed: %s", exc)
        return

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                d = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            yield d
    finally:
        try:
            await scanner.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ble scan stop failed: %s", exc)
