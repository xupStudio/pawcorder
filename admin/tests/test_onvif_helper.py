"""Tests for app.onvif_helper.

We never hit the network: every test wires httpx.MockTransport into a
local AsyncClient and monkey-patches httpx.AsyncClient so the helper's
internal `async with httpx.AsyncClient(...)` picks up our transport.
"""
from __future__ import annotations

import base64
from typing import Callable

import httpx
import pytest

from app import onvif_helper


# ---- helpers ------------------------------------------------------------

def _install_transport(monkeypatch: pytest.MonkeyPatch,
                       handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """Patch httpx.AsyncClient so any new instance routes through `handler`.
    Returns a list that captures every request the helper issues."""
    captured: list[httpx.Request] = []

    def _capture(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    transport = httpx.MockTransport(_capture)
    real = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return captured


def _soap(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


_DEVICE_INFO_XML = _soap(
    "<tds:GetDeviceInformationResponse>"
    "<tds:Manufacturer>AcmeCam</tds:Manufacturer>"
    "<tds:Model>AC-9000</tds:Model>"
    "<tds:FirmwareVersion>1.2.3</tds:FirmwareVersion>"
    "<tds:SerialNumber>SN-XYZ-001</tds:SerialNumber>"
    "</tds:GetDeviceInformationResponse>"
)

_GET_SERVICES_XML = _soap(
    "<tds:GetServicesResponse>"
    "<tds:Service>"
    "<tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace>"
    "<tds:XAddr>http://1.2.3.4/onvif/device_service</tds:XAddr>"
    "</tds:Service>"
    "<tds:Service>"
    "<tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>"
    "<tds:XAddr>http://1.2.3.4/onvif/Media</tds:XAddr>"
    "</tds:Service>"
    "</tds:GetServicesResponse>"
)

_GET_PROFILES_XML = _soap(
    "<trt:GetProfilesResponse>"
    '<trt:Profiles token="MainStream"><tt:Name>main</tt:Name>'
    "<tt:VideoEncoderConfiguration><tt:Resolution>"
    "<tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>"
    "</tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>"
    '<trt:Profiles token="SubStream"><tt:Name>sub</tt:Name>'
    "<tt:VideoEncoderConfiguration><tt:Resolution>"
    "<tt:Width>640</tt:Width><tt:Height>360</tt:Height>"
    "</tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>"
    "</trt:GetProfilesResponse>"
)


def _stream_uri_xml(uri: str) -> str:
    return _soap(
        "<trt:GetStreamUriResponse><trt:MediaUri>"
        f"<tt:Uri>{uri}</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
        "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
        "<tt:Timeout>PT60S</tt:Timeout>"
        "</trt:MediaUri></trt:GetStreamUriResponse>"
    )


# ---- 1. digest math -----------------------------------------------------

def test_password_digest_matches_known_vector():
    """Hand-computed vector: SHA1(nonce16 + created + 'hunter2')."""
    nonce = bytes(range(1, 17))   # 0x01..0x10
    created = "2024-01-02T03:04:05Z"
    expected = "oXj8RNP94D8Kg5Mf6/2f2np9JWY="
    assert onvif_helper._password_digest(nonce, created, "hunter2") == expected
    # Sanity: the components round-trip.
    assert base64.b64encode(nonce).decode("ascii") == "AQIDBAUGBwgJCgsMDQ4PEA=="


# ---- 2. GetDeviceInformation parsing ------------------------------------

@pytest.mark.asyncio
async def test_get_device_information_parses_response(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_DEVICE_INFO_XML,
                              headers={"Content-Type": "application/soap+xml"})

    _install_transport(monkeypatch, handler)
    info = await onvif_helper.get_device_information("1.2.3.4", "u", "p")
    assert info == {
        "manufacturer": "AcmeCam",
        "model": "AC-9000",
        "firmware_version": "1.2.3",
        "serial_number": "SN-XYZ-001",
    }


# ---- 3. get_stream_uri picks first profile ------------------------------

def _multi_handler(routes: dict[str, str]) -> Callable[[httpx.Request], httpx.Response]:
    """Pick a response based on which SOAP action the request contains."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode("utf-8", errors="ignore")
        for needle, xml in routes.items():
            if needle in body:
                return httpx.Response(200, text=xml,
                                      headers={"Content-Type": "application/soap+xml"})
        return httpx.Response(500, text="<no-match/>")
    return handler


@pytest.mark.asyncio
async def test_get_stream_uri_picks_first_profile_when_unspecified(monkeypatch):
    routes = {
        "GetServices":  _GET_SERVICES_XML,
        "GetProfiles":  _GET_PROFILES_XML,
        "GetStreamUri": _stream_uri_xml("rtsp://1.2.3.4:554/main"),
    }
    _install_transport(monkeypatch, _multi_handler(routes))
    url = await onvif_helper.get_stream_uri("1.2.3.4", "u", "p")
    assert url == "rtsp://u:p@1.2.3.4:554/main"


# ---- 4. explicit profile_token is forwarded -----------------------------

@pytest.mark.asyncio
async def test_get_stream_uri_uses_specified_profile_token(monkeypatch):
    routes = {
        "GetServices":  _GET_SERVICES_XML,
        "GetStreamUri": _stream_uri_xml("rtsp://1.2.3.4:554/sub"),
    }
    captured = _install_transport(monkeypatch, _multi_handler(routes))
    url = await onvif_helper.get_stream_uri(
        "1.2.3.4", "u", "p", profile_token="SubStream",
    )
    assert url == "rtsp://u:p@1.2.3.4:554/sub"

    # GetProfiles must NOT have been called when token is explicit.
    bodies = [r.content.decode("utf-8", errors="ignore") for r in captured]
    assert not any("GetProfiles" in b and "GetProfilesResponse" not in b for b in bodies)
    # The explicit token must appear in the GetStreamUri request body.
    stream_bodies = [b for b in bodies if "GetStreamUri" in b]
    assert stream_bodies and "<trt:ProfileToken>SubStream</trt:ProfileToken>" in stream_bodies[0]


# ---- 5. WS-Security 401 -> Digest fallback succeeds ---------------------

@pytest.mark.asyncio
async def test_auto_configure_falls_back_to_digest_auth_on_401_ws_security(monkeypatch):
    """First attempt to each endpoint returns 401; retry (via DigestAuth) succeeds.
    httpx.DigestAuth handles a 401+WWW-Authenticate transparently, so from the
    transport's POV it sees: req -> 401(challenge), req(with Authorization) -> 200."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode("utf-8", errors="ignore")
        # If the helper sent a WS-Security envelope (no Authorization header),
        # reject with plain 401 to force the helper to retry under DigestAuth.
        if "wsse:Security" in body:
            return httpx.Response(401, text="unauthorized")
        # Now under httpx.DigestAuth: first call has no Authorization,
        # we challenge; second call carries Authorization, we succeed.
        if "Authorization" not in req.headers:
            return httpx.Response(
                401, text="challenge",
                headers={"WWW-Authenticate": 'Digest realm="onvif", '
                         'nonce="abc", qop="auth", algorithm=MD5'},
            )
        if "GetDeviceInformation" in body:
            return httpx.Response(200, text=_DEVICE_INFO_XML)
        if "GetServices" in body:
            return httpx.Response(200, text=_GET_SERVICES_XML)
        if "GetProfiles" in body:
            return httpx.Response(200, text=_GET_PROFILES_XML)
        if "GetStreamUri" in body:
            # Pick a different uri per profile token so we can tell main/sub apart.
            uri = ("rtsp://1.2.3.4:554/main"
                   if "MainStream" in body else "rtsp://1.2.3.4:554/sub")
            return httpx.Response(200, text=_stream_uri_xml(uri))
        return httpx.Response(500, text="unhandled")

    _install_transport(monkeypatch, handler)
    out = await onvif_helper.auto_configure("1.2.3.4", "u", "p")
    assert out["device"]["manufacturer"] == "AcmeCam"
    assert out["connection_type"] == "unknown"
    assert out["link"] is None
    assert out["rtsp_main"] == "rtsp://u:p@1.2.3.4:554/main"
    assert out["rtsp_sub"] == "rtsp://u:p@1.2.3.4:554/sub"


# ---- 6. invalid creds raise PermissionError -----------------------------

@pytest.mark.asyncio
async def test_invalid_credentials_raise_permission_error(monkeypatch):
    """Both WS-Security and Digest paths return 401 — helper must raise."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, text="nope",
            headers={"WWW-Authenticate": 'Digest realm="onvif", '
                     'nonce="abc", qop="auth", algorithm=MD5'},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(PermissionError):
        await onvif_helper.get_device_information("1.2.3.4", "u", "p")
