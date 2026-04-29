"""Tests for the Axis VAPIX helper. httpx is mocked via ``MockTransport``
so no real network is ever touched."""
from __future__ import annotations

from typing import Callable

import httpx
import pytest

from app import axis_api
from app.axis_api import auto_configure, rtsp_url


# --- canned VAPIX fixtures ----------------------------------------------

def _basic_device_info_json(prod_nbr: str = "M1065-LW") -> dict:
    """VAPIX ``basicdeviceinfo.cgi`` response with a swappable model number."""
    return {
        "apiVersion": "1.0",
        "data": {
            "propertyList": {
                "ProdNbr": prod_nbr,
                "ProdShortName": f"AXIS {prod_nbr}",
                "SerialNumber": "ACCC8E000000",
                "Version": "10.12.190",
            },
        },
    }


PARAM_CGI_TEXT = (
    "root.Brand.Brand=AXIS\n"
    "root.Brand.ProdNbr=M1054\n"
    "root.Brand.ProdShortName=AXIS M1054\n"
    "root.Properties.Firmware.Version=5.40.9.2\n"
    "root.Properties.System.SerialNumber=00408C123456\n"
)


# --- mock-transport helpers ---------------------------------------------

def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Force ``axis_api`` to build clients with our mock transport. Returns
    a list capturing every request issued -- useful for endpoint asserts."""
    captured: list[httpx.Request] = []

    def _record_and_handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("auth", None)  # Digest auth is irrelevant to the mock transport
        kwargs["transport"] = httpx.MockTransport(_record_and_handle)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(axis_api.httpx, "AsyncClient", _factory)
    return captured


def test_rtsp_url_main_and_sub():
    assert rtsp_url("1.2.3.4", "root", "pw") == \
        "rtsp://root:pw@1.2.3.4:554/axis-media/media.amp"
    assert rtsp_url("1.2.3.4", "root", "pw", sub=True) == \
        "rtsp://root:pw@1.2.3.4:554/axis-media/media.amp?resolution=320x240"


def test_rtsp_url_url_encodes_password():
    url = rtsp_url("1.2.3.4", "ro:ot", "p@ss:word")
    # ``:`` and ``@`` inside the userinfo must be percent-encoded or an
    # RTSP client will mis-parse the credential boundary.
    assert "ro%3Aot" in url
    assert "p%40ss%3Aword" in url
    assert "@1.2.3.4:554/axis-media/media.amp" in url


@pytest.mark.asyncio
async def test_auto_configure_uses_basicdeviceinfo_when_available(monkeypatch):
    # M1065-L is the wired/PoE sibling of -LW -- exercise the wired branch
    # explicitly so this test doesn't double-cover the WiFi-detect tests.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/axis-cgi/basicdeviceinfo.cgi":
            return httpx.Response(200, json=_basic_device_info_json("M1065-L"))
        return httpx.Response(404)

    captured = _patch_client(monkeypatch, handler)
    out = await auto_configure("10.0.0.7", "root", "secret")

    # The JSON-RPC endpoint must be hit via POST -- a GET would 405.
    posts = [r for r in captured if r.url.path == "/axis-cgi/basicdeviceinfo.cgi"]
    assert posts, "expected a request to basicdeviceinfo.cgi"
    assert all(r.method == "POST" for r in posts)
    # And the param.cgi fallback must NOT have been called.
    assert not any(r.url.path == "/axis-cgi/param.cgi" for r in captured)

    assert out["device"] == {
        "manufacturer": "Axis",
        "model": "M1065-L",
        "firmware_version": "10.12.190",
        "serial": "ACCC8E000000",
    }
    assert out["link"] is None
    assert out["connection_type"] == "wired"
    assert out["rtsp_main"] == "rtsp://root:secret@10.0.0.7:554/axis-media/media.amp"
    assert out["rtsp_sub"].endswith("?resolution=320x240")


@pytest.mark.asyncio
async def test_auto_configure_detects_wifi_for_LW_models(monkeypatch):
    # Axis ``-LW`` suffix means built-in WiFi -- onboarding should flag the
    # cam so the UI can route it through the WPS handshake instead of asking
    # the user to plug in an Ethernet cable.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/axis-cgi/basicdeviceinfo.cgi":
            return httpx.Response(200, json=_basic_device_info_json("M1065-LW"))
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    out = await auto_configure("10.0.0.7", "root", "secret")

    assert out["device"]["model"] == "M1065-LW"
    assert out["connection_type"] == "wifi"


@pytest.mark.asyncio
async def test_auto_configure_detects_wifi_for_WV_models(monkeypatch):
    # ``-WV`` is the outdoor weatherproof + WiFi variant. Same wifi flag as
    # ``-LW``; the suffix matcher must accept both.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/axis-cgi/basicdeviceinfo.cgi":
            return httpx.Response(200, json=_basic_device_info_json("M3045-WV"))
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    out = await auto_configure("10.0.0.7", "root", "secret")

    assert out["device"]["model"] == "M3045-WV"
    assert out["connection_type"] == "wifi"


@pytest.mark.asyncio
async def test_auto_configure_falls_back_to_param_cgi_on_404(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/axis-cgi/basicdeviceinfo.cgi":
            return httpx.Response(404)  # legacy firmware -- endpoint absent
        if request.url.path == "/axis-cgi/param.cgi":
            return httpx.Response(
                200, text=PARAM_CGI_TEXT,
                headers={"Content-Type": "text/plain"},
            )
        return httpx.Response(500)

    captured = _patch_client(monkeypatch, handler)
    out = await auto_configure("10.0.0.8", "root", "secret")

    paths = [r.url.path for r in captured]
    assert "/axis-cgi/basicdeviceinfo.cgi" in paths
    assert "/axis-cgi/param.cgi" in paths

    assert out["device"] == {
        "manufacturer": "Axis",
        "model": "M1054",
        "firmware_version": "5.40.9.2",
        "serial": "00408C123456",
    }
    assert out["connection_type"] == "wired"


@pytest.mark.asyncio
async def test_invalid_credentials_raise_permission_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        # VAPIX returns 401 with a WWW-Authenticate header on bad creds;
        # for the mock we only care about the status code path.
        return httpx.Response(401, text="Unauthorized")

    _patch_client(monkeypatch, handler)
    with pytest.raises(PermissionError):
        await auto_configure("10.0.0.9", "root", "wrong")


@pytest.mark.asyncio
async def test_timeout_raises_runtime_error_with_ip(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("read timed out", request=request)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await auto_configure("10.0.0.99", "root", "secret")
    assert "10.0.0.99" in str(excinfo.value)
