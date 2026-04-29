"""Hikvision ISAPI auto-configuration helper.

Hikvision IP cameras (and many of their OEM rebrands -- Annke, LTS, Hilook,
Safire, etc.) expose a standardised XML REST API at /ISAPI/... protected by
HTTP Digest auth. We use it to:

  * read DeviceInfo (model / firmware / serial / MAC),
  * ensure the main video stream (channel 101) is enabled,
  * read Network/Interfaces to classify the link as wifi vs wired.

This mirrors the public surface of ``camera_api.auto_configure`` so the
admin panel's onboarding flow can treat Reolink and Hikvision cameras
interchangeably.

Only stdlib + httpx is used, so the licence footprint stays MIT/BSD/Apache.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from . import camera_compat
from .camera_utils import VendorHttpErrors, xml_find_text, xml_localname

# 8s matches the Reolink helper -- enough for slow PoE cameras to wake up,
# short enough that a misconfigured IP fails the onboarding wizard quickly.
_TIMEOUT = httpx.Timeout(8.0)


# --- URL helpers ----------------------------------------------------------

def rtsp_url(
    ip: str,
    user: str,
    password: str,
    *,
    channel: int = 1,
    sub: bool = False,
    port: int = 554,
) -> str:
    """Build a Hikvision RTSP URL.

    Thin wrapper over ``camera_compat.build_rtsp_url("hikvision", ...)``
    that handles the channel arithmetic (``Streaming/Channels/101`` etc.)
    and URL-encodes credentials.
    """
    return camera_compat.build_rtsp_url(
        "hikvision", ip, user, password, port=port, sub=sub, channel=channel,
    )


# --- ISAPI calls ----------------------------------------------------------

async def _get_device_info(client: httpx.AsyncClient, base_url: str) -> dict[str, Any]:
    """GET /ISAPI/System/deviceInfo and pluck the fields we care about."""
    resp = await client.get(f"{base_url}/ISAPI/System/deviceInfo")
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    return {
        "manufacturer": "Hikvision",
        "model": xml_find_text(root, "model") or None,
        "firmware_version": xml_find_text(root, "firmwareVersion") or None,
        "serial": xml_find_text(root, "serialNumber") or None,
        "mac": xml_find_text(root, "macAddress") or None,
        "device_name": xml_find_text(root, "deviceName") or None,
    }


async def _ensure_main_stream_enabled(client: httpx.AsyncClient, base_url: str) -> None:
    """Read channel 101's config and PUT it back with ``<enabled>true</enabled>``
    if the camera reports the main stream as disabled. Hikvision insists on
    the full original XML being echoed back -- partial PUTs are rejected --
    so we do a string-level toggle instead of a parse/serialise round-trip
    (ElementTree mangles default namespaces into ``ns0:`` prefixes which
    some firmware then refuses).
    """
    url = f"{base_url}/ISAPI/Streaming/channels/101"
    resp = await client.get(url)
    resp.raise_for_status()
    text = resp.text
    root = ET.fromstring(text)

    enabled_el = next(
        (el for el in root.iter() if xml_localname(el.tag) == "enabled"), None,
    )
    if enabled_el is None or (enabled_el.text or "").strip().lower() == "true":
        return

    # Replace the first <enabled>false</enabled> token, tolerant of casing
    # and inner whitespace ("<enabled> False </enabled>" appears on some
    # firmware). regex preserves the namespace prefix if present.
    body, count = re.subn(
        r"<(\w*:?enabled)>\s*false\s*</\1>",
        r"<\1>true</\1>",
        text, count=1, flags=re.IGNORECASE,
    )
    if count == 0:
        # Conservative fallback: if the regex didn't match, leave the camera
        # alone — it's better to fall through and let the user toggle in the
        # Hikvision web UI than to PUT a body that doesn't differ.
        return
    put = await client.put(url, content=body.encode("utf-8"),
                           headers={"Content-Type": "application/xml"})
    put.raise_for_status()


async def _get_link_classification(client: httpx.AsyncClient, base_url: str) -> str:
    """Inspect /ISAPI/Network/Interfaces/1 and return ``wifi``/``wired``/``unknown``.

    Hikvision exposes ``<linkType>`` (``Auto``, ``100M-FD``, ...) plus a
    ``<Wireless>`` element when wifi is configured. We err on the side of
    ``wired`` because the vast majority of Hikvision cams are PoE-only;
    only an explicit Wireless block flips us to wifi.
    """
    try:
        resp = await client.get(f"{base_url}/ISAPI/Network/Interfaces/1")
        resp.raise_for_status()
    except Exception:  # noqa: BLE001 -- link info is informational, never load-bearing
        return "unknown"

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return "unknown"

    has_wireless = any(xml_localname(el.tag) == "Wireless" for el in root.iter())
    if has_wireless:
        return "wifi"

    link_type = xml_find_text(root, "linkType")
    if link_type:
        lt = link_type.lower()
        if "wireless" in lt or "wifi" in lt:
            return "wifi"
        return "wired"
    return "unknown"


# --- public entry point ---------------------------------------------------

async def auto_configure(
    ip: str,
    user: str,
    password: str,
    *,
    port: int = 80,
) -> dict[str, Any]:
    """Read ISAPI device info, ensure the main stream is on, classify the link.

    Returns a dict with ``device``, ``link``, ``connection_type``,
    ``rtsp_main`` and ``rtsp_sub`` keys -- shape parity with
    ``camera_api.auto_configure`` so the onboarding flow is camera-agnostic.

    Raises:
        PermissionError: HTTP 401 -- bad credentials.
        RuntimeError:    timeout / connect error / unexpected ISAPI failure.
    """
    base_url = f"http://{ip}:{port}"
    auth = httpx.DigestAuth(user, password)
    async with VendorHttpErrors("Hikvision", ip):
        async with httpx.AsyncClient(auth=auth, timeout=_TIMEOUT) as client:
            # First call must run alone so a 401/403 surfaces before we
            # fan out to parallel calls (cleaner test pyramid + smaller
            # blast radius if creds are wrong).
            device = await _get_device_info(client, base_url)

            async def _try_enable_main() -> None:
                # Best-effort: NVR-attached cams sometimes serve channel
                # 101 under a different ID and 404 here. Don't fail the
                # whole onboarding for it — the user can still record.
                try:
                    await _ensure_main_stream_enabled(client, base_url)
                except httpx.HTTPStatusError as inner:
                    if inner.response.status_code != 404:
                        raise

            # ensure-main and link-classification are independent reads;
            # gather to save one round-trip per onboarding.
            _, link_type = await asyncio.gather(
                _try_enable_main(),
                _get_link_classification(client, base_url),
            )

    return {
        "device": device,
        "link": {"link_type": link_type} if link_type != "unknown" else None,
        "connection_type": link_type,
        "rtsp_main": rtsp_url(ip, user, password),
        "rtsp_sub": rtsp_url(ip, user, password, sub=True),
    }
