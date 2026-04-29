"""Dahua HTTP CGI auto-config helper (also used for Amcrest, a Dahua OEM).

Dahua and Amcrest cameras share the same CGI surface at /cgi-bin/. Auth is
HTTP Digest. Unlike Reolink, RTSP is on by default on Dahua firmware, so this
helper is mostly a *read-only* probe: it pulls device info via magicBox.cgi,
inspects the active network interface (eth vs wireless), and verifies the
main encode profile is enabled. The setConfig call to flip Encode on is rare
in practice — most units ship with main+sub already on.

We deliberately keep this module dependency-light: stdlib + httpx only.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import camera_compat
from .camera_utils import VendorHttpErrors, parse_kv_text


_TIMEOUT_SECONDS = 8.0


# ---- URL builders --------------------------------------------------------

def rtsp_url(
    ip: str,
    user: str,
    password: str,
    *,
    sub: bool = False,
    port: int = 554,
) -> str:
    """Build a Dahua/Amcrest RTSP URL via the shared brand template.

    Stream selection rides on the ``subtype=`` query param. Pawcorder
    addresses one camera per IP, so we don't expose a ``channel`` kwarg —
    the brand template's hard-coded ``channel=1`` is the only path used.
    """
    return camera_compat.build_rtsp_url(
        "dahua", ip, user, password, port=port, sub=sub,
    )


# ---- Async CGI helpers ---------------------------------------------------

async def _cgi_get(client: httpx.AsyncClient, path: str) -> str:
    resp = await client.get(path)
    resp.raise_for_status()
    return resp.text


async def _get_system_info(client: httpx.AsyncClient, base_url: str) -> dict[str, str]:
    """Fetch deviceType, serialNumber, hardwareVersion via magicBox.cgi."""
    text = await _cgi_get(client, "/cgi-bin/magicBox.cgi?action=getSystemInfo")
    return parse_kv_text(text)


async def _get_software_version(client: httpx.AsyncClient, base_url: str) -> str:
    """Return firmware version string, or '' if the field is missing."""
    text = await _cgi_get(client, "/cgi-bin/magicBox.cgi?action=getSoftwareVersion")
    kv = parse_kv_text(text)
    return kv.get("version", "")


async def _get_interfaces(client: httpx.AsyncClient, base_url: str) -> list[dict[str, str]]:
    """Return a list of interface dicts parsed from netApp.cgi.

    The CGI response looks like:
        table.NetWorkInterface[0].Name=eth0
        table.NetWorkInterface[0].Type=eth
        table.NetWorkInterface[1].Name=wlan0
        table.NetWorkInterface[1].Type=wireless
    We bucket entries by their numeric index and produce a list of dicts.
    """
    try:
        text = await _cgi_get(client, "/cgi-bin/netApp.cgi?action=getInterfaces")
    except httpx.HTTPError:
        return []
    kv = parse_kv_text(text)
    buckets: dict[int, dict[str, str]] = {}
    for full_key, value in kv.items():
        # full_key example: table.NetWorkInterface[0].Type
        if "[" not in full_key or "]" not in full_key:
            continue
        try:
            idx = int(full_key.split("[", 1)[1].split("]", 1)[0])
        except ValueError:
            continue
        leaf = full_key.rsplit(".", 1)[-1]
        buckets.setdefault(idx, {})[leaf] = value
    return [buckets[i] for i in sorted(buckets)]


async def _ensure_main_stream_enabled(client: httpx.AsyncClient, base_url: str) -> None:
    """Verify the main encode profile is on; flip it via setConfig if not.

    Tolerates a missing/garbled Encode response (firmware variants differ) —
    in that case we simply skip the toggle. RTSP itself is governed by a
    different service that is on by default; this is belt-and-braces.
    """
    try:
        text = await _cgi_get(
            client, "/cgi-bin/configManager.cgi?action=getConfig&name=Encode"
        )
    except httpx.HTTPError:
        return
    kv = parse_kv_text(text)
    flag = kv.get("table.Encode[0].MainFormat[0].Video.enabled")
    if flag is None:
        return  # field absent — assume default-on, don't touch it
    if flag.lower() in ("true", "1"):
        return
    try:
        await _cgi_get(
            client,
            "/cgi-bin/configManager.cgi?action=setConfig"
            "&Encode[0].MainFormat[0].Video.enabled=true",
        )
    except httpx.HTTPError:
        # Non-fatal: the camera will still stream sub even if main is off.
        return


# ---- Classification ------------------------------------------------------

def _classify_interfaces(interfaces: list[dict[str, str]]) -> str:
    """Map a list of Dahua netApp interfaces to 'wifi' | 'wired' | 'unknown'.

    Preference order: a wireless interface with a non-empty IP wins. Falling
    back to any wireless type, then any eth type, then 'unknown'. Dahua does
    not distinguish PoE vs adapter-fed Ethernet, and Frigate doesn't care.
    """
    if not interfaces:
        return "unknown"
    wireless = [i for i in interfaces if i.get("Type", "").lower() == "wireless"]
    wired = [i for i in interfaces if i.get("Type", "").lower() == "eth"]
    # If a wireless iface has an IP it is the active link.
    for iface in wireless:
        ip = iface.get("IPAddress") or iface.get("Address") or ""
        if ip and ip != "0.0.0.0":
            return "wifi"
    if wireless and not wired:
        return "wifi"
    if wired:
        return "wired"
    return "unknown"


def _detect_manufacturer(system_info: dict[str, str]) -> str:
    """Guess Dahua vs Amcrest from device fields. Defaults to 'Dahua'."""
    blob = " ".join(system_info.values()).lower()
    if "amcrest" in blob:
        return "Amcrest"
    return "Dahua"


# ---- Public entry point --------------------------------------------------

async def auto_configure(
    ip: str,
    user: str,
    password: str,
    *,
    port: int = 80,
) -> dict[str, Any]:
    """Probe a Dahua/Amcrest camera and return device + link metadata.

    Mirrors the shape returned by ``camera_api.auto_configure`` so callers
    can treat the two helpers polymorphically.

    Raises:
        PermissionError: invalid credentials (HTTP 401).
        RuntimeError: connect/timeout/HTTP failure (message includes IP).
    """
    base_url = f"http://{ip}:{port}"
    auth = httpx.DigestAuth(user, password)
    async with VendorHttpErrors("Dahua", ip):
        async with httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            timeout=_TIMEOUT_SECONDS,
        ) as client:
            # First call must run alone so a 401/403 surfaces before we
            # fan out to parallel calls (cleaner failure isolation if creds
            # are wrong — VendorHttpErrors maps the status code).
            system_info = await _get_system_info(client, base_url)

            # All three are independent reads on the same authenticated client;
            # gather them so onboarding doesn't wait for 3 sequential RTTs.
            firmware, _, interfaces = await asyncio.gather(
                _get_software_version(client, base_url),
                _ensure_main_stream_enabled(client, base_url),
                _get_interfaces(client, base_url),
            )

    manufacturer = _detect_manufacturer(system_info)
    device = {
        "model": system_info.get("deviceType", ""),
        "firmware_version": firmware,
        "serial": system_info.get("serialNumber", ""),
        "manufacturer": manufacturer,
    }
    return {
        "device": device,
        "link": {"interfaces": interfaces} if interfaces else None,
        "connection_type": _classify_interfaces(interfaces),
        "rtsp_main": rtsp_url(ip, user, password, sub=False),
        "rtsp_sub": rtsp_url(ip, user, password, sub=True),
    }
