"""Tests for the Foscam CGI helper.

We mock httpx with a route-based ``MockTransport`` so no real network is
ever touched -- the CGI XML payloads are inlined as canned fixtures.
"""
from __future__ import annotations

from typing import Callable

import httpx
import pytest

from app import foscam_api
from app.foscam_api import auto_configure, rtsp_url

# --- canned CGI fixtures --------------------------------------------------

DEVINFO_OK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CGI_Result>
  <result>0</result>
  <productName>FI9821W</productName>
  <devName>kitchen-cam</devName>
  <mac>00:11:22:33:44:55</mac>
  <hardwareVer>2.7.1.6</hardwareVer>
  <firmwareVer>2.84.2.34</firmwareVer>
  <serialNo>FOSCAM-ABC-1234567890</serialNo>
</CGI_Result>
"""

DEVINFO_BAD_CREDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CGI_Result><result>1</result></CGI_Result>
"""


# --- mock-transport helpers ----------------------------------------------

def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Force ``foscam_api`` to build clients with our mock transport.

    Returns a list capturing every request made -- useful for asserting
    on URL/port/query-string shape.
    """
    captured: list[httpx.Request] = []

    def _record_and_handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_record_and_handle)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(foscam_api.httpx, "AsyncClient", _factory)
    return captured


def _ok(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"Content-Type": "text/xml"})


# --- pure URL builder tests ----------------------------------------------

def test_rtsp_url_main_and_sub():
    assert rtsp_url("1.2.3.4", "admin", "pw") == \
        "rtsp://admin:pw@1.2.3.4:554/videoMain"
    assert rtsp_url("1.2.3.4", "admin", "pw", sub=True) == \
        "rtsp://admin:pw@1.2.3.4:554/videoSub"


def test_rtsp_url_url_encodes_password():
    url = rtsp_url("1.2.3.4", "ad:min", "p@ss:word")
    # ``:`` and ``@`` inside userinfo must be percent-encoded, otherwise
    # an RTSP client mis-parses the userinfo section.
    assert "ad%3Amin" in url
    assert "p%40ss%3Aword" in url
    assert url.endswith("@1.2.3.4:554/videoMain")


# --- auto_configure ------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_configure_returns_expected_shape(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/CGIProxy.fcgi":
            return _ok(DEVINFO_OK_XML)
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    out = await auto_configure("10.0.0.7", "admin", "secret")

    assert out["device"]["manufacturer"] == "Foscam"
    assert out["device"]["model"] == "FI9821W"
    assert out["device"]["device_name"] == "kitchen-cam"
    assert out["device"]["mac"] == "00:11:22:33:44:55"
    assert out["device"]["hardware_version"] == "2.7.1.6"
    assert out["device"]["firmware_version"] == "2.84.2.34"
    assert out["device"]["serial"] == "FOSCAM-ABC-1234567890"
    # Foscam CGI doesn't expose link info -- caller gets unknowns, not lies.
    assert out["link"] is None
    assert out["connection_type"] == "unknown"
    assert out["rtsp_main"] == "rtsp://admin:secret@10.0.0.7:554/videoMain"
    assert out["rtsp_sub"] == "rtsp://admin:secret@10.0.0.7:554/videoSub"


@pytest.mark.asyncio
async def test_auto_configure_uses_port_88_by_default(monkeypatch):
    """Foscam's web CGI listens on 88, not 80, on most firmware. The
    default port must reflect that or onboarding fails on a fresh unit."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(DEVINFO_OK_XML)

    captured = _patch_client(monkeypatch, handler)
    await auto_configure("10.0.0.7", "admin", "secret")

    assert len(captured) == 1
    req = captured[0]
    assert req.url.host == "10.0.0.7"
    assert req.url.port == 88
    # CGI command + creds must be query-string params (per Foscam spec).
    assert req.url.params.get("cmd") == "getDevInfo"
    assert req.url.params.get("usr") == "admin"
    assert req.url.params.get("pwd") == "secret"


@pytest.mark.asyncio
async def test_invalid_credentials_raise_permission_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        # Foscam returns HTTP 200 with <result>1</result> on bad creds --
        # the auth signal is in the XML body, not the status code.
        return _ok(DEVINFO_BAD_CREDS_XML)

    _patch_client(monkeypatch, handler)
    with pytest.raises(PermissionError):
        await auto_configure("10.0.0.7", "admin", "wrong")


@pytest.mark.asyncio
async def test_timeout_raises_runtime_error_with_ip(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("read timed out", request=request)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await auto_configure("10.0.0.99", "admin", "secret")
    assert "10.0.0.99" in str(excinfo.value)
