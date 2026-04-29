"""Tests for the Hikvision ISAPI helper.

We mock httpx with a route-based ``MockTransport`` so no real network is
ever touched -- the ISAPI XML payloads are inlined as canned fixtures.
"""
from __future__ import annotations

from typing import Callable

import httpx
import pytest

from app import hikvision_api
from app.hikvision_api import auto_configure, rtsp_url

# --- canned ISAPI fixtures -----------------------------------------------

NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'

DEVICE_INFO_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo {NS}>
  <deviceName>front-door</deviceName>
  <model>DS-2CD2143G2-I</model>
  <firmwareVersion>V5.7.3</firmwareVersion>
  <serialNumber>DS-XYZ-20240101AAWR1234567890</serialNumber>
  <macAddress>aa:bb:cc:dd:ee:ff</macAddress>
</DeviceInfo>
"""

CHANNEL_101_ENABLED_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<StreamingChannel {NS}>
  <id>101</id>
  <channelName>Camera 01</channelName>
  <enabled>true</enabled>
  <Video><videoCodecType>H.264</videoCodecType></Video>
</StreamingChannel>
"""

CHANNEL_101_DISABLED_XML = CHANNEL_101_ENABLED_XML.replace(
    "<enabled>true</enabled>", "<enabled>false</enabled>",
)

NETWORK_WIRED_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<NetworkInterface {NS}>
  <id>1</id>
  <IPAddress><ipVersion>v4</ipVersion><ipAddress>192.168.1.50</ipAddress></IPAddress>
  <Link><linkType>Auto</linkType><speed>100</speed><duplex>full</duplex></Link>
</NetworkInterface>
"""

NETWORK_WIFI_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<NetworkInterface {NS}>
  <id>1</id>
  <IPAddress><ipVersion>v4</ipVersion><ipAddress>192.168.1.50</ipAddress></IPAddress>
  <Wireless><enabled>true</enabled><ssid>home-wifi</ssid></Wireless>
</NetworkInterface>
"""


# --- mock-transport helpers ----------------------------------------------

def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient backed by a MockTransport routing handler."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(8.0))


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """Force ``hikvision_api`` to build clients with our mock transport.

    Returns a list that captures every request made -- useful for asserting
    that a PUT happened (or didn't).
    """
    captured: list[httpx.Request] = []

    def _record_and_handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("auth", None)  # Digest auth is irrelevant to the mock transport
        kwargs["transport"] = httpx.MockTransport(_record_and_handle)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(hikvision_api.httpx, "AsyncClient", _factory)
    return captured


def _ok(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"Content-Type": "application/xml"})


# --- pure URL builder tests ----------------------------------------------

def test_rtsp_url_main_and_sub():
    assert rtsp_url("1.2.3.4", "admin", "pw") == \
        "rtsp://admin:pw@1.2.3.4:554/Streaming/Channels/101"
    assert rtsp_url("1.2.3.4", "admin", "pw", sub=True) == \
        "rtsp://admin:pw@1.2.3.4:554/Streaming/Channels/102"
    assert rtsp_url("1.2.3.4", "admin", "pw", channel=2) == \
        "rtsp://admin:pw@1.2.3.4:554/Streaming/Channels/201"


def test_rtsp_url_url_encodes_password():
    url = rtsp_url("1.2.3.4", "ad:min", "p@ss:word")
    # The literal ``:`` and ``@`` must be percent-encoded inside credentials,
    # otherwise an RTSP client mis-parses the userinfo section.
    assert "ad%3Amin" in url
    assert "p%40ss%3Aword" in url
    assert url.endswith("@1.2.3.4:554/Streaming/Channels/101")


# --- auto_configure ------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_configure_returns_expected_shape(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ISAPI/System/deviceInfo":
            return _ok(DEVICE_INFO_XML)
        if path == "/ISAPI/Streaming/channels/101":
            return _ok(CHANNEL_101_ENABLED_XML)
        if path == "/ISAPI/Network/Interfaces/1":
            return _ok(NETWORK_WIFI_XML)
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    out = await auto_configure("10.0.0.5", "admin", "secret")

    assert out["device"]["manufacturer"] == "Hikvision"
    assert out["device"]["model"] == "DS-2CD2143G2-I"
    assert out["device"]["firmware_version"] == "V5.7.3"
    assert out["device"]["serial"].startswith("DS-XYZ-")
    assert out["device"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert out["connection_type"] == "wifi"
    assert out["link"] == {"link_type": "wifi"}
    assert out["rtsp_main"] == "rtsp://admin:secret@10.0.0.5:554/Streaming/Channels/101"
    assert out["rtsp_sub"] == "rtsp://admin:secret@10.0.0.5:554/Streaming/Channels/102"


@pytest.mark.asyncio
async def test_auto_configure_classifies_wired(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ISAPI/System/deviceInfo":
            return _ok(DEVICE_INFO_XML)
        if request.url.path == "/ISAPI/Streaming/channels/101":
            return _ok(CHANNEL_101_ENABLED_XML)
        if request.url.path == "/ISAPI/Network/Interfaces/1":
            return _ok(NETWORK_WIRED_XML)
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    out = await auto_configure("10.0.0.5", "admin", "secret")
    assert out["connection_type"] == "wired"
    assert out["link"] == {"link_type": "wired"}


@pytest.mark.asyncio
async def test_auto_configure_enables_main_stream_when_disabled(monkeypatch):
    """If channel 101 reports ``enabled=false``, we must PUT it back as ``true``
    before returning, otherwise Frigate sees a black stream."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ISAPI/System/deviceInfo":
            return _ok(DEVICE_INFO_XML)
        if path == "/ISAPI/Streaming/channels/101":
            if request.method == "GET":
                return _ok(CHANNEL_101_DISABLED_XML)
            return httpx.Response(200)  # PUT -> 200 OK
        if path == "/ISAPI/Network/Interfaces/1":
            return _ok(NETWORK_WIRED_XML)
        return httpx.Response(404)

    captured = _patch_client(monkeypatch, handler)
    await auto_configure("10.0.0.5", "admin", "secret")

    puts = [r for r in captured if r.method == "PUT" and r.url.path == "/ISAPI/Streaming/channels/101"]
    assert len(puts) == 1, "expected a single PUT to re-enable channel 101"
    body = puts[0].content.decode("utf-8")
    assert "<enabled>true</enabled>" in body
    assert "<enabled>false</enabled>" not in body


@pytest.mark.asyncio
async def test_invalid_credentials_raise_permission_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        # ISAPI returns 401 with an XML errorCode body when digest auth fails.
        return httpx.Response(401, text="<ResponseStatus><statusCode>4</statusCode></ResponseStatus>")

    _patch_client(monkeypatch, handler)
    with pytest.raises(PermissionError):
        await auto_configure("10.0.0.5", "admin", "wrong")


@pytest.mark.asyncio
async def test_timeout_raises_runtime_error_with_useful_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("read timed out", request=request)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await auto_configure("10.0.0.99", "admin", "secret")
    assert "10.0.0.99" in str(excinfo.value)
