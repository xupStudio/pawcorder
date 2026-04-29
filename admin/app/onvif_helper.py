"""ONVIF Profile S minimal client.

Hand-crafted SOAP over httpx — no zeep dependency, no C extensions, no
WS-Discovery (pawcorder runs its own subnet scan in network_scan.py and
hands us an IP). We support exactly what the admin onboarding flow
needs: identify the device, list its media profiles, and resolve an
RTSP URL with credentials embedded.

Auth fallback order: WS-UsernameToken digest first; on HTTP 401 we
retry with httpx.DigestAuth. If both fail we raise PermissionError so
the route layer can surface a 'bad credentials' message.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx

_TIMEOUT = 8.0
_CONTENT_TYPE = "application/soap+xml; charset=utf-8"

_NS = {
    "s":    "http://www.w3.org/2003/05/soap-envelope",
    "tds":  "http://www.onvif.org/ver10/device/wsdl",
    "trt":  "http://www.onvif.org/ver10/media/wsdl",
    "tt":   "http://www.onvif.org/ver10/schema",
}

_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"'
    ' xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"'
    ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema">'
    "<s:Header>{security}</s:Header>"
    "<s:Body>{body}</s:Body>"
    "</s:Envelope>"
)


# ---- WS-Security UsernameToken ------------------------------------------

def _password_digest(nonce: bytes, created: str, password: str) -> str:
    """PasswordDigest = base64(sha1(nonce + created + password)).

    `nonce` is the *raw* bytes (not the base64 form). `created` is the
    ISO-8601 UTC timestamp string. Matches the OASIS WSS UsernameToken
    Profile 1.0 algorithm used by every ONVIF camera in the wild.
    """
    h = hashlib.sha1()
    h.update(nonce + created.encode("utf-8") + password.encode("utf-8"))
    return base64.b64encode(h.digest()).decode("ascii")


def _build_security_header(user: str, password: str) -> str:
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = _password_digest(nonce, created, password)
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    return (
        "<wsse:Security s:mustUnderstand=\"1\">"
        "<wsse:UsernameToken>"
        f"<wsse:Username>{user}</wsse:Username>"
        f"<wsse:Password Type=\"http://docs.oasis-open.org/wss/2004/01/"
        f"oasis-200401-wss-username-token-profile-1.0#PasswordDigest\">{digest}</wsse:Password>"
        f"<wsse:Nonce EncodingType=\"http://docs.oasis-open.org/wss/2004/01/"
        f"oasis-200401-wss-soap-message-security-1.0#Base64Binary\">{nonce_b64}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken>"
        "</wsse:Security>"
    )


# ---- SOAP transport ------------------------------------------------------

async def _soap_call(
    client: httpx.AsyncClient,
    url: str,
    body: str,
    action: str,
    user: str,
    password: str,
) -> ET.Element:
    """POST a SOAP request. Tries WS-UsernameToken first, falls back to
    HTTP Digest auth on 401. Returns the parsed <s:Envelope> root.

    Sends both the action-in-Content-Type form (SOAP 1.2 idiomatic) and a
    separate `SOAPAction:` header — some older Hikvision/Vivotek/Hanwha
    firmware honours only the latter.
    """
    headers = {
        "Content-Type": f'{_CONTENT_TYPE}; action="{action}"',
        "SOAPAction": f'"{action}"',
    }
    envelope = _ENVELOPE.format(
        security=_build_security_header(user, password),
        body=body,
    )
    resp = await client.post(url, content=envelope, headers=headers)

    if resp.status_code == 401:
        # Retry without the WS-Security header, using HTTP Digest auth.
        bare = _ENVELOPE.format(security="", body=body)
        resp = await client.post(
            url, content=bare, headers=headers, auth=httpx.DigestAuth(user, password),
        )
        if resp.status_code == 401:
            raise PermissionError("invalid ONVIF credentials")

    resp.raise_for_status()
    return ET.fromstring(resp.text)


def _txt(elem: ET.Element | None, path: str) -> str:
    """Find a child by namespaced path, return text or empty string."""
    if elem is None:
        return ""
    found = elem.find(path, _NS)
    return (found.text or "").strip() if found is not None and found.text else ""


# ---- Public API ----------------------------------------------------------

async def get_device_information(
    ip: str, user: str, password: str, port: int = 80,
) -> dict[str, str]:
    """GetDeviceInformation SOAP call.

    Returns a dict with keys manufacturer / model / firmware_version /
    serial_number. Missing fields are returned as empty strings rather
    than omitted, so callers can render the form fields unconditionally.
    """
    url = f"http://{ip}:{port}/onvif/device_service"
    body = "<tds:GetDeviceInformation/>"
    action = "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        root = await _soap_call(client, url, body, action, user, password)
    resp = root.find(".//tds:GetDeviceInformationResponse", _NS)
    return {
        "manufacturer":     _txt(resp, "tds:Manufacturer"),
        "model":            _txt(resp, "tds:Model"),
        "firmware_version": _txt(resp, "tds:FirmwareVersion"),
        "serial_number":    _txt(resp, "tds:SerialNumber"),
    }


async def _get_services(client: httpx.AsyncClient, ip: str, port: int,
                        user: str, password: str) -> str:
    """Return the Media XAddr advertised by GetServices, or a sensible default."""
    url = f"http://{ip}:{port}/onvif/device_service"
    body = "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>"
    action = "http://www.onvif.org/ver10/device/wsdl/GetServices"
    try:
        root = await _soap_call(client, url, body, action, user, password)
    except (httpx.HTTPError, ET.ParseError):
        return f"http://{ip}:{port}/onvif/Media"
    for svc in root.findall(".//tds:Service", _NS):
        ns = _txt(svc, "tds:Namespace")
        if ns == "http://www.onvif.org/ver10/media/wsdl":
            xaddr = _txt(svc, "tds:XAddr")
            if xaddr:
                return xaddr
    return f"http://{ip}:{port}/onvif/Media"


def _parse_profiles(root: ET.Element) -> list[dict[str, Any]]:
    """Pull (token, name, width, height) tuples out of a GetProfilesResponse."""
    profiles: list[dict[str, Any]] = []
    for p in root.findall(".//trt:Profiles", _NS):
        token = p.attrib.get("token", "")
        name = _txt(p, "tt:Name")
        res = p.find(".//tt:Resolution", _NS)
        width = int(_txt(res, "tt:Width") or 0) if res is not None else 0
        height = int(_txt(res, "tt:Height") or 0) if res is not None else 0
        profiles.append({"token": token, "name": name,
                         "width": width, "height": height})
    return profiles


async def _list_profiles(client: httpx.AsyncClient, media_url: str,
                         user: str, password: str) -> list[dict[str, Any]]:
    body = "<trt:GetProfiles/>"
    action = "http://www.onvif.org/ver10/media/wsdl/GetProfiles"
    root = await _soap_call(client, media_url, body, action, user, password)
    return _parse_profiles(root)


async def _resolve_uri(client: httpx.AsyncClient, media_url: str,
                       token: str, user: str, password: str) -> str:
    body = (
        "<trt:GetStreamUri>"
        "<trt:StreamSetup>"
        "<tt:Stream>RTP-Unicast</tt:Stream>"
        "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
        "</trt:StreamSetup>"
        f"<trt:ProfileToken>{token}</trt:ProfileToken>"
        "</trt:GetStreamUri>"
    )
    action = "http://www.onvif.org/ver10/media/wsdl/GetStreamUri"
    root = await _soap_call(client, media_url, body, action, user, password)
    return _txt(root, ".//trt:MediaUri/tt:Uri")


def _embed_credentials(rtsp_uri: str, user: str, password: str) -> str:
    """Insert user:pass@ into an rtsp:// URL. If creds are already there
    (some firmware does this) leave it alone. URL-encodes user/password so
    special characters like ':' or '@' don't corrupt the netloc — same
    discipline as camera_api.rtsp_url for Reolink."""
    if not rtsp_uri:
        return ""
    parsed = urlparse(rtsp_uri)
    if parsed.username:
        return rtsp_uri
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parsed.netloc}"
    return parsed._replace(netloc=netloc).geturl()


async def get_stream_uri(
    ip: str, user: str, password: str, port: int = 80,
    *, profile_token: str | None = None,
) -> str:
    """Resolve an RTSP URL via ONVIF.

    Steps: GetServices -> GetProfiles -> GetStreamUri. If
    ``profile_token`` is None, the first profile is used. Returned URL
    has the credentials embedded, ready to hand to Frigate / ffmpeg.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        media_url = await _get_services(client, ip, port, user, password)
        if profile_token is None:
            profiles = await _list_profiles(client, media_url, user, password)
            if not profiles:
                raise RuntimeError("ONVIF camera reported zero media profiles")
            profile_token = profiles[0]["token"]
        uri = await _resolve_uri(client, media_url, profile_token, user, password)
    return _embed_credentials(uri, user, password)


async def auto_configure(
    ip: str, user: str, password: str, port: int = 80,
) -> dict[str, Any]:
    """Top-level entry mirroring camera_api.auto_configure's shape.

    Picks the highest-resolution profile as 'main' and the lowest as
    'sub'. If only one profile exists, ``rtsp_sub`` is an empty string.
    'link' / 'connection_type' are stubbed because ONVIF Profile S
    doesn't expose link type — that's a vendor extension.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Device info via the device service. GetDeviceInformation and
        # GetServices are independent SOAP calls — gather to save 1 RTT.
        dev_url = f"http://{ip}:{port}/onvif/device_service"

        async def _device_info() -> dict:
            dev_root = await _soap_call(
                client, dev_url, "<tds:GetDeviceInformation/>",
                "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
                user, password,
            )
            dev = dev_root.find(".//tds:GetDeviceInformationResponse", _NS)
            return {
                "manufacturer":     _txt(dev, "tds:Manufacturer"),
                "model":            _txt(dev, "tds:Model"),
                "firmware_version": _txt(dev, "tds:FirmwareVersion"),
                "serial_number":    _txt(dev, "tds:SerialNumber"),
            }

        device, media_url = await asyncio.gather(
            _device_info(),
            _get_services(client, ip, port, user, password),
        )

        profiles = await _list_profiles(client, media_url, user, password)
        if not profiles:
            raise RuntimeError("ONVIF camera reported zero media profiles")

        # Highest resolution = main; lowest = sub. Width*Height as ranking key.
        ranked = sorted(profiles, key=lambda p: p["width"] * p["height"])
        sub_p = ranked[0]
        main_p = ranked[-1]

        # Resolve main + sub stream URIs in parallel (independent media-service
        # calls; identical-token case is fast-pathed below).
        if sub_p["token"] == main_p["token"]:
            main_uri = await _resolve_uri(client, media_url, main_p["token"], user, password)
            sub_uri = ""
        else:
            main_uri, sub_uri = await asyncio.gather(
                _resolve_uri(client, media_url, main_p["token"], user, password),
                _resolve_uri(client, media_url, sub_p["token"], user, password),
            )

    return {
        "device": device,
        "link": None,
        "connection_type": "unknown",
        "rtsp_main": _embed_credentials(main_uri, user, password),
        "rtsp_sub":  _embed_credentials(sub_uri, user, password) if sub_uri else "",
    }
