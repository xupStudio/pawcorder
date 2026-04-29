"""Tests for the Dahua/Amcrest CGI helper.

We mock httpx with a route-based MockTransport — no real network. The
helper opens its own AsyncClient, so we monkeypatch httpx.AsyncClient to
hand it a transport-backed client that records every request.
"""
from __future__ import annotations

from typing import Callable

import httpx
import pytest

from app import dahua_api
from app.camera_utils import parse_kv_text as _parse_kv_text
from app.dahua_api import auto_configure, rtsp_url


# ---- Fixtures / helpers --------------------------------------------------

_SYSINFO_TEXT = (
    "deviceType=IPC-HFW1230S2\r\n"
    "serialNumber=ABC123XYZ\r\n"
    "hardwareVersion=1.00\r\n"
)
_SOFTWARE_TEXT = "version=2.800.0000000.27.R\r\n"
_ENCODE_ENABLED = (
    "table.Encode[0].MainFormat[0].Video.enabled=true\r\n"
    "table.Encode[0].MainFormat[0].Video.Width=1920\r\n"
)
_ENCODE_DISABLED = (
    "table.Encode[0].MainFormat[0].Video.enabled=false\r\n"
    "table.Encode[0].MainFormat[0].Video.Width=1920\r\n"
)
_INTERFACES_WIRED = (
    "table.NetWorkInterface[0].Name=eth0\r\n"
    "table.NetWorkInterface[0].Type=eth\r\n"
    "table.NetWorkInterface[0].IPAddress=192.168.1.50\r\n"
    "table.NetWorkInterface[1].Name=wlan0\r\n"
    "table.NetWorkInterface[1].Type=wireless\r\n"
    "table.NetWorkInterface[1].IPAddress=0.0.0.0\r\n"
)


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Monkeypatch httpx.AsyncClient so the helper uses a MockTransport.

    Returns a list that is appended to with each captured request, so
    individual tests can assert on call counts and target paths.
    """
    captured: list[httpx.Request] = []

    def _wrapped(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    transport = httpx.MockTransport(_wrapped)
    real_cls = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        # Drop digest auth — MockTransport doesn't replay the 401 challenge.
        kwargs.pop("auth", None)
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(dahua_api.httpx, "AsyncClient", _factory)
    return captured


def _ok_handler(routes: dict[str, str]) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that maps `path?query` substrings to text bodies."""
    def _h(req: httpx.Request) -> httpx.Response:
        full = req.url.path + ("?" + req.url.query.decode() if req.url.query else "")
        for needle, body in routes.items():
            if needle in full:
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="not found")
    return _h


# ---- Pure-function tests -------------------------------------------------

def test_rtsp_url_main_and_sub():
    assert rtsp_url("1.2.3.4", "admin", "pw") == (
        "rtsp://admin:pw@1.2.3.4:554/cam/realmonitor?channel=1&subtype=0"
    )
    assert rtsp_url("1.2.3.4", "admin", "pw", sub=True) == (
        "rtsp://admin:pw@1.2.3.4:554/cam/realmonitor?channel=1&subtype=1"
    )


def test_rtsp_url_url_encodes_password():
    # `:` must become %3A, `@` must become %40 — otherwise consumers parse
    # the credentials wrong and the auth half splits at the wrong place.
    url = rtsp_url("10.0.0.1", "user", "p:a@ss/word")
    assert "p%3Aa%40ss%2Fword" in url
    assert url.startswith("rtsp://user:p%3Aa%40ss%2Fword@10.0.0.1:554/")


def test_parse_kv_text_handles_dotted_keys():
    text = (
        "Encode[0].MainFormat[0].Video.enabled=true\r\n"
        "\r\n"
        "deviceType=IPC-HFW1230S2\r\n"
        "noisy line without an equals sign\r\n"
    )
    kv = _parse_kv_text(text)
    assert kv["Encode[0].MainFormat[0].Video.enabled"] == "true"
    assert kv["deviceType"] == "IPC-HFW1230S2"
    assert "noisy line without an equals sign" not in kv


# ---- auto_configure tests ------------------------------------------------

@pytest.mark.asyncio
async def test_auto_configure_returns_expected_shape(monkeypatch):
    handler = _ok_handler({
        "magicBox.cgi?action=getSystemInfo": _SYSINFO_TEXT,
        "magicBox.cgi?action=getSoftwareVersion": _SOFTWARE_TEXT,
        "magicBox.cgi?action=getMachineName": "name=front-door\r\n",
        "configManager.cgi?action=getConfig&name=Encode": _ENCODE_ENABLED,
        "netApp.cgi?action=getInterfaces": _INTERFACES_WIRED,
    })
    _install_transport(monkeypatch, handler)

    result = await auto_configure("192.168.1.50", "admin", "pw")

    assert set(result.keys()) == {
        "device", "link", "connection_type", "rtsp_main", "rtsp_sub",
    }
    assert result["device"]["model"] == "IPC-HFW1230S2"
    assert result["device"]["serial"] == "ABC123XYZ"
    assert result["device"]["firmware_version"].startswith("2.800")
    assert result["device"]["manufacturer"] == "Dahua"
    assert result["connection_type"] == "wired"
    assert result["link"] is not None and len(result["link"]["interfaces"]) == 2
    assert "subtype=0" in result["rtsp_main"]
    assert "subtype=1" in result["rtsp_sub"]


@pytest.mark.asyncio
async def test_auto_configure_calls_setConfig_when_main_disabled(monkeypatch):
    handler = _ok_handler({
        "magicBox.cgi?action=getSystemInfo": _SYSINFO_TEXT,
        "magicBox.cgi?action=getSoftwareVersion": _SOFTWARE_TEXT,
        "configManager.cgi?action=getConfig&name=Encode": _ENCODE_DISABLED,
        "configManager.cgi?action=setConfig": "OK\r\n",
        "netApp.cgi?action=getInterfaces": _INTERFACES_WIRED,
    })
    captured = _install_transport(monkeypatch, handler)

    await auto_configure("192.168.1.50", "admin", "pw")

    set_calls = [
        r for r in captured
        if "configManager.cgi" in r.url.path
        and b"setConfig" in r.url.query
    ]
    assert set_calls, "expected a setConfig call when Encode main is disabled"
    assert b"Video.enabled=true" in set_calls[0].url.query


@pytest.mark.asyncio
async def test_invalid_credentials_raise_permission_error(monkeypatch):
    def _h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    _install_transport(monkeypatch, _h)

    with pytest.raises(PermissionError):
        await auto_configure("192.168.1.99", "admin", "wrong")


@pytest.mark.asyncio
async def test_timeout_raises_runtime_error_with_ip(monkeypatch):
    def _h(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=req)

    _install_transport(monkeypatch, _h)

    with pytest.raises(RuntimeError) as excinfo:
        await auto_configure("10.20.30.40", "admin", "pw")
    assert "10.20.30.40" in str(excinfo.value)
