"""Foscam CGI auto-configuration helper.

Foscam IP cameras expose ``/cgi-bin/CGIProxy.fcgi`` taking command + creds
as **query-string parameters** -- e.g. ``?cmd=getDevInfo&usr=...&pwd=...``.
This is the documented Foscam API; cleartext creds in the URL are per spec.

Security: cleartext creds in the URL are acceptable on a LAN. For remote
access, pawcorder fronts the admin panel with Tailscale -- the WireGuard
tunnel encrypts the whole exchange end-to-end, so the cleartext URL never
leaves the host.

RTSP auth: modern Foscam firmware defaults to ``digest``. We deliberately
do **not** call ``setRtspAuthType`` -- toggling it forces existing clients
(Frigate, VLC) to reconnect. We only read it for diagnostics.

Only stdlib + httpx is used (MIT/BSD/Apache licence footprint).
"""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

import httpx

from . import camera_compat
from .camera_utils import VendorHttpErrors, xml_find_text

# 8s matches the Reolink/Hikvision helpers -- enough for a slow camera to
# wake up, short enough that a misconfigured IP fails fast.
_TIMEOUT = httpx.Timeout(8.0)


# --- URL helpers ----------------------------------------------------------

def rtsp_url(
    ip: str,
    user: str,
    password: str,
    *,
    sub: bool = False,
    port: int = 554,
) -> str:
    """Build a Foscam RTSP URL via the shared brand template.

    ``videoMain`` (full-res) and ``videoSub`` (thumbnail-res) are the
    standard Foscam paths. Note: Foscam's *web CGI* listens on port 88,
    but RTSP is on the standard 554 — this helper builds the RTSP URL
    only, so the default is 554, not 88.
    """
    return camera_compat.build_rtsp_url(
        "foscam", ip, user, password, port=port, sub=sub,
    )


def _parse_devinfo(root: ET.Element) -> dict[str, Any]:
    """Pull the device-identity fields out of a getDevInfo CGI_Result."""
    return {
        "manufacturer": "Foscam",
        "model": xml_find_text(root, "productName") or None,
        "device_name": xml_find_text(root, "devName") or None,
        "mac": xml_find_text(root, "mac") or None,
        "hardware_version": xml_find_text(root, "hardwareVer") or None,
        "firmware_version": xml_find_text(root, "firmwareVer") or None,
        "serial": xml_find_text(root, "serialNo") or None,
    }


# --- CGI calls ------------------------------------------------------------

async def _cgi_call(
    client: httpx.AsyncClient,
    base_url: str,
    cmd: str,
    user: str,
    password: str,
) -> ET.Element:
    """Issue one CGIProxy.fcgi GET and return the parsed ``<CGI_Result>``.

    ``<result>0</result>`` = OK; ``1`` = bad creds (PermissionError); any
    other non-zero value is a generic CGI error (RuntimeError).
    """
    resp = await client.get(
        f"{base_url}/cgi-bin/CGIProxy.fcgi",
        params={"cmd": cmd, "usr": user, "pwd": password},
    )
    # Some Foscam firmware returns 401/403 directly when the URL params
    # are wrong; normalise to PermissionError before the XML parse.
    if resp.status_code in (401, 403):
        raise PermissionError(f"Foscam auth rejected for cmd={cmd}")
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    result = xml_find_text(root, "result", default="")
    # Foscam CGI result codes: 0 = OK, 1 = bad credentials, -2 = no
    # permission, -3 = param error (which often surfaces as auth-equivalent
    # because the missing perm prevents the param from being read).
    if result in ("1", "-2"):
        raise PermissionError(f"Foscam auth rejected for cmd={cmd} (result={result})")
    if result not in ("", "0"):
        raise RuntimeError(f"Foscam cmd={cmd} returned result={result}")
    return root


# --- public entry point ---------------------------------------------------

async def auto_configure(
    ip: str,
    user: str,
    password: str,
    *,
    port: int = 88,
) -> dict[str, Any]:
    """Read Foscam device info via CGI; return a normalised dict.

    Default port is **88** (Foscam web UI), not 80. Shape matches
    ``camera_api.auto_configure`` so the onboarding wizard is
    camera-agnostic. ``link`` is None / ``connection_type='unknown'``
    because Foscam CGI has no equivalent of Hikvision's Network/Interfaces.

    Raises:
        PermissionError: ``<result>1</result>`` -- bad credentials.
        RuntimeError:    timeout, connect error, or non-zero CGI result.
    """
    base_url = f"http://{ip}:{port}"
    async with VendorHttpErrors("Foscam", ip):
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            root = await _cgi_call(client, base_url, "getDevInfo", user, password)

    return {
        "device": _parse_devinfo(root),
        "link": None,
        "connection_type": "unknown",
        "rtsp_main": rtsp_url(ip, user, password),
        "rtsp_sub": rtsp_url(ip, user, password, sub=True),
    }
