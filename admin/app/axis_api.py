"""VAPIX auto-config helper for Axis cameras.

Axis cameras enable RTSP by default, so this module is mostly a
health-check + device-info read. We try ``basicdeviceinfo.cgi`` (JSON-RPC,
VAPIX 3+) first and fall back to ``param.cgi?action=list`` (key=value
text) for older firmwares. Auth is HTTP Digest -- Axis supports Basic +
Digest and Digest is the modern default. Only stdlib + httpx is used so
the licence footprint stays MIT/BSD/Apache. Public surface mirrors
``camera_api.auto_configure`` so onboarding stays vendor-agnostic.

Why not the upstream ``axis`` PyPI lib (Home Assistant's choice)?
We tried. ``axis>=63`` installs cleanly on Python 3.13 and exposes a
clean ``AxisDevice``/``Vapix.initialize()`` async API on top of httpx
(no extra aiohttp -- contrary to a common assumption). The blocker is
its mandatory transitive ``faust-cchardet``, which is tri-licensed
MPL 1.1 / GPL / LGPL. Pawcorder's licence policy permits only
MIT / BSD / Apache 2.0 / MPL 2.0 / OFL, so the lib is off-limits until
upstream drops that dep. The hand-rolled VAPIX call below is ~30 lines
of httpx, so the maintenance cost is negligible compared to the licence
footprint we'd inherit.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import camera_compat
from .camera_utils import VendorHttpErrors, parse_kv_text

# 8s matches the other vendor helpers -- enough for a slow PoE camera to
# wake up, short enough to fail a misconfigured IP without hanging the UI.
_TIMEOUT = httpx.Timeout(8.0)

# Axis encodes the radio in the model-number suffix:
#   * ``-LW``  : built-in 2.4/5 GHz WiFi (e.g. M1065-LW)
#   * ``-WV``  : weatherproof + WiFi (e.g. M3045-WV)
# Anything else (``-L`` low-light, ``-V`` vandal, no suffix) is wired/PoE only.
# Suffix match is case-insensitive because firmware reports vary in casing.
_WIFI_MODEL_SUFFIXES = ("LW", "WV")


def _connection_type_for_model(model: str | None) -> str:
    """Infer wired vs. wifi from the Axis product number suffix."""
    if not model:
        return "wired"
    upper = model.upper()
    return "wifi" if upper.endswith(_WIFI_MODEL_SUFFIXES) else "wired"


def rtsp_url(
    ip: str,
    user: str,
    password: str,
    *,
    sub: bool = False,
    port: int = 554,
) -> str:
    """Build an Axis ``axis-media`` RTSP URL via the shared brand template.

    Sub-stream selection rides on ``resolution=`` -- the most portable
    knob across firmwares (``streamprofile=`` requires a pre-defined
    profile on the device). The brand template encodes that already; this
    function just URL-encodes credentials and substitutes IP/port through
    the shared builder.
    """
    return camera_compat.build_rtsp_url(
        "axis", ip, user, password, port=port, sub=sub,
    )


async def _get_basicdeviceinfo(
    client: httpx.AsyncClient, base_url: str,
) -> dict[str, Any] | None:
    """JSON-RPC device-info endpoint. Returns ``None`` on 404 so the caller
    can fall back to ``param.cgi``; other HTTP errors propagate."""
    payload = {"apiVersion": "1.0", "method": "getAllProperties"}
    resp = await client.post(f"{base_url}/axis-cgi/basicdeviceinfo.cgi", json=payload)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    props = ((resp.json().get("data") or {}).get("propertyList")) or {}
    return {
        "manufacturer": "Axis",
        "model": props.get("ProdNbr") or props.get("ProdShortName"),
        "firmware_version": props.get("Version"),
        "serial": props.get("SerialNumber"),
    }


async def _get_param_cgi(
    client: httpx.AsyncClient, base_url: str,
) -> dict[str, Any]:
    """Legacy fallback: scrape the ``key=value`` text from ``param.cgi``.

    Shares the parser with ``dahua_api`` (same line grammar, both vendors
    flatten dotted/bracketed config keys into a single text response).
    """
    resp = await client.get(f"{base_url}/axis-cgi/param.cgi", params={"action": "list"})
    resp.raise_for_status()
    params = parse_kv_text(resp.text)
    return {
        "manufacturer": "Axis",
        "model": params.get("root.Brand.ProdNbr") or params.get("root.Brand.ProdShortName"),
        "firmware_version": params.get("root.Properties.Firmware.Version"),
        "serial": params.get("root.Properties.System.SerialNumber"),
    }


async def auto_configure(
    ip: str,
    user: str,
    password: str,
    *,
    port: int = 80,
) -> dict[str, Any]:
    """Talk to Axis VAPIX, read device info, hand back canonical RTSP URLs.

    Tries ``basicdeviceinfo.cgi`` first; falls back to ``param.cgi`` on 404.
    RTSP is on by default on Axis cameras, so nothing is toggled.
    ``connection_type`` is inferred from the model-number suffix: ``-LW``
    or ``-WV`` -> ``wifi``, anything else -> ``wired``. ``link`` stays
    ``None`` for shape parity with the other vendor helpers (we don't have
    a stable VAPIX endpoint for RSSI/SSID across firmwares).

    Raises:
        PermissionError: HTTP 401 / 403 -- bad credentials.
        RuntimeError:    timeout / connect error / unexpected VAPIX error.
    """
    base_url = f"http://{ip}:{port}"
    auth = httpx.DigestAuth(user, password)
    async with VendorHttpErrors("Axis", ip):
        async with httpx.AsyncClient(auth=auth, timeout=_TIMEOUT) as client:
            device = await _get_basicdeviceinfo(client, base_url)
            if device is None:
                device = await _get_param_cgi(client, base_url)

    return {
        "device": device,
        "link": None,
        "connection_type": _connection_type_for_model(device.get("model")),
        "rtsp_main": rtsp_url(ip, user, password),
        "rtsp_sub": rtsp_url(ip, user, password, sub=True),
    }
