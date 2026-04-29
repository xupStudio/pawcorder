"""Tests for the UniFi Protect controller helper.

The helper delegates almost everything to ``uiprotect.ProtectApiClient``
— we don't second-guess uiprotect's HTTP behaviour. Instead we mock the
client class itself with ``unittest.mock`` so we can assert on:

  * which kwargs we'd hand to ``ProtectApiClient(...)`` (host/port/
    verify_ssl in particular);
  * how we react to its ``.bootstrap`` shape (cameras → channels →
    rtsp_alias);
  * that we call its ``set_rtsp_enabled`` (or fall back to
    ``queue_update``) when a channel was off;
  * that ``NotAuthorized`` propagates as ``PermissionError``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import unifi_api
from app.unifi_api import (
    _is_private_host,
    auto_configure,
    rtsp_url,
)


# ---- Fixtures ------------------------------------------------------------

CONTROLLER = "https://192.168.1.1"
CONTROLLER_HOST = "192.168.1.1"


def _make_channel(*, rtsp_enabled: bool, alias: str) -> MagicMock:
    """Mock a uiprotect ``CameraChannel``.

    The fields we touch on the real model are ``is_rtsp_enabled`` and
    ``rtsp_alias``. We use plain attributes (not spec=) so the mock
    can be mutated by our `queue_update` callbacks if exercised.
    """
    ch = MagicMock()
    ch.is_rtsp_enabled = rtsp_enabled
    ch.rtsp_alias = alias if rtsp_enabled else ""
    return ch


def _make_camera(
    *,
    cam_id: str = "cam-A",
    name: str = "Front Door",
    mac: str = "AA:BB:CC:DD:EE:FF",
    host: str = "192.168.1.50",
    model: str = "G4 Pro",
    rtsp_enabled: bool = True,
    main_alias: str = "mainAlias123",
    sub_alias: str = "subAlias456",
) -> MagicMock:
    """Mock a uiprotect ``Camera`` with two channels."""
    cam = MagicMock()
    cam.id = cam_id
    cam.name = name
    cam.mac = mac
    cam.host = host
    cam.market_name = model
    cam.type = model
    cam.channels = [
        _make_channel(rtsp_enabled=rtsp_enabled, alias=main_alias),
        _make_channel(rtsp_enabled=rtsp_enabled, alias=sub_alias),
    ]

    # Awaitable setter — when our wrapper calls it, flip the
    # corresponding channel's flag + alias so the post-enable shape is
    # what auto_configure() should observe.
    async def _set_rtsp_enabled(idx: int, on: bool) -> None:
        channel = cam.channels[idx]
        channel.is_rtsp_enabled = on
        if on and not channel.rtsp_alias:
            channel.rtsp_alias = main_alias if idx == 0 else sub_alias

    cam.set_rtsp_enabled = AsyncMock(side_effect=_set_rtsp_enabled)
    cam.queue_update = AsyncMock()
    return cam


def _make_bootstrap(cameras: list[MagicMock], *, version: str = "2.11.21") -> MagicMock:
    bootstrap = MagicMock()
    bootstrap.cameras = {c.id: c for c in cameras}
    bootstrap.nvr = MagicMock()
    bootstrap.nvr.version = version
    return bootstrap


def _make_client_factory(
    cameras: list[MagicMock],
    *,
    update_side_effect: Exception | None = None,
    capture: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a fake ``ProtectApiClient`` class.

    The returned MagicMock acts like the *class* — calling it returns
    a fresh "instance" mock. ``capture`` records the kwargs used at
    instantiation so tests can assert on host/port/verify_ssl.
    """

    def _ctor(**kwargs: Any) -> MagicMock:
        if capture is not None:
            capture.update(kwargs)
        instance = MagicMock()
        if update_side_effect is not None:
            instance.update = AsyncMock(side_effect=update_side_effect)
        else:
            instance.update = AsyncMock(return_value=None)
        instance.bootstrap = _make_bootstrap(cameras)
        instance.close_session = AsyncMock()
        return instance

    factory = MagicMock(side_effect=_ctor)
    return factory


# ---- Pure helpers --------------------------------------------------------

def test_is_private_host_recognises_rfc1918() -> None:
    assert _is_private_host("https://192.168.1.1") is True
    assert _is_private_host("https://10.0.0.5:8443") is True
    assert _is_private_host("https://172.16.5.4") is True
    assert _is_private_host("https://172.31.255.254") is True
    assert _is_private_host("https://8.8.8.8") is False
    # Public IP just outside the 172.16/12 block.
    assert _is_private_host("https://172.32.0.1") is False
    # Hostnames are not IPs and therefore not "private".
    assert _is_private_host("https://protect.example.com") is False


def test_rtsp_url_with_alias() -> None:
    assert rtsp_url("192.168.1.1", "abc123") == "rtsp://192.168.1.1:7447/abc123"
    assert rtsp_url("192.168.1.1", "abc123", port=7448) == "rtsp://192.168.1.1:7448/abc123"


def test_rtsp_url_empty_alias_returns_empty() -> None:
    assert rtsp_url("192.168.1.1", "") == ""
    assert rtsp_url("192.168.1.1", "", port=7448) == ""


# ---- auto_configure happy paths -----------------------------------------

@pytest.mark.asyncio
async def test_auto_configure_lists_cameras() -> None:
    cams = [
        _make_camera(cam_id="cam-A", name="Front Door"),
        _make_camera(
            cam_id="cam-B",
            name="Back Yard",
            main_alias="mainB",
            sub_alias="subB",
        ),
    ]
    factory = _make_client_factory(cams)

    with patch.object(unifi_api, "ProtectApiClient", factory):
        out = await auto_configure(CONTROLLER, "admin", "pw")

    # Top-level shape — both the legacy keys and the brand-dispatcher
    # keys must be populated.
    assert out["device"]["manufacturer"] == "Ubiquiti UniFi Protect"
    assert out["device"]["controller_version"] == "2.11.21"
    assert out["link"] is None
    assert out["connection_type"] == "wired"

    assert len(out["cameras"]) == 2
    first = out["cameras"][0]
    assert first["id"] == "cam-A"
    assert first["name"] == "Front Door"
    assert first["host"] == "192.168.1.50"
    assert first["model"] == "G4 Pro"
    assert first["rtsp_main"] == f"rtsp://{CONTROLLER_HOST}:7447/mainAlias123"
    assert first["rtsp_sub"] == f"rtsp://{CONTROLLER_HOST}:7447/subAlias456"

    # Top-level rtsp_main / rtsp_sub mirror the FIRST camera so the
    # dispatcher contract is satisfied.
    assert out["rtsp_main"] == first["rtsp_main"]
    assert out["rtsp_sub"] == first["rtsp_sub"]

    # No RTSP enable should have fired — both cameras already had it on.
    for cam in cams:
        cam.set_rtsp_enabled.assert_not_called()
        cam.queue_update.assert_not_called()


@pytest.mark.asyncio
async def test_auto_configure_enables_rtsp_when_disabled() -> None:
    cam = _make_camera(cam_id="cam-A", rtsp_enabled=False)
    factory = _make_client_factory([cam])

    with patch.object(unifi_api, "ProtectApiClient", factory):
        out = await auto_configure(CONTROLLER, "admin", "pw")

    # Both channels were off → both got toggled on, in order.
    assert cam.set_rtsp_enabled.await_count == 2
    called_indices = [call.args[0] for call in cam.set_rtsp_enabled.await_args_list]
    called_flags = [call.args[1] for call in cam.set_rtsp_enabled.await_args_list]
    assert called_indices == [0, 1]
    assert called_flags == [True, True]

    # And the resulting URLs reflect the freshly-minted aliases.
    assert out["cameras"][0]["rtsp_main"].endswith("/mainAlias123")
    assert out["cameras"][0]["rtsp_sub"].endswith("/subAlias456")


@pytest.mark.asyncio
async def test_auto_configure_falls_back_to_queue_update_when_no_setter() -> None:
    """When the camera object lacks ``set_rtsp_enabled`` (older uiprotect
    releases) we should fall back to ``queue_update``."""
    cam = _make_camera(cam_id="cam-A", rtsp_enabled=False)
    # Drop the explicit setter so the wrapper takes the queue_update path.
    del cam.set_rtsp_enabled
    factory = _make_client_factory([cam])

    with patch.object(unifi_api, "ProtectApiClient", factory):
        await auto_configure(CONTROLLER, "admin", "pw")

    # queue_update fired once per disabled channel.
    assert cam.queue_update.await_count == 2
    # The callback flips is_rtsp_enabled on the channel passed by closure.
    for call in cam.queue_update.await_args_list:
        callback = call.args[0]
        callback()  # idempotent — already True after first run, but that's fine
    for ch in cam.channels:
        assert ch.is_rtsp_enabled is True


# ---- Error mapping -------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_credentials_raise_permission_error() -> None:
    from uiprotect.exceptions import NotAuthorized

    factory = _make_client_factory(
        [], update_side_effect=NotAuthorized("bad creds")
    )

    with patch.object(unifi_api, "ProtectApiClient", factory):
        with pytest.raises(PermissionError):
            await auto_configure(CONTROLLER, "admin", "wrong")


# ---- TLS verify-flag wiring ---------------------------------------------

@pytest.mark.asyncio
async def test_self_signed_cert_accepted_for_private_host() -> None:
    """Private host -> verify_ssl=False; public hostname -> verify_ssl=True."""
    # Private host
    private_capture: dict[str, Any] = {}
    factory = _make_client_factory([], capture=private_capture)
    with patch.object(unifi_api, "ProtectApiClient", factory):
        await auto_configure("https://192.168.1.1", "admin", "pw")
    assert private_capture["verify_ssl"] is False
    assert private_capture["host"] == "192.168.1.1"
    # default port when none in URL
    assert private_capture["port"] == 443

    # Public hostname
    public_capture: dict[str, Any] = {}
    factory = _make_client_factory([], capture=public_capture)
    with patch.object(unifi_api, "ProtectApiClient", factory):
        await auto_configure("https://protect.example.com:8443", "admin", "pw")
    assert public_capture["verify_ssl"] is True
    assert public_capture["host"] == "protect.example.com"
    assert public_capture["port"] == 8443

    # Public IP — verify_ssl=True too
    pub_ip_capture: dict[str, Any] = {}
    factory = _make_client_factory([], capture=pub_ip_capture)
    with patch.object(unifi_api, "ProtectApiClient", factory):
        await auto_configure("https://8.8.8.8", "admin", "pw")
    assert pub_ip_capture["verify_ssl"] is True
