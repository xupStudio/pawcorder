"""UniFi Protect controller helper, backed by the ``uiprotect`` library.

This module is a thin async wrapper around ``uiprotect.ProtectApiClient``
(MIT-licensed, the maintained successor to ``pyunifiprotect`` used by
Home Assistant). uiprotect handles the authentication dance, websocket
bootstrap, firmware-quirk normalisation and pydantic-typed device
objects; we only translate its world into the dict shape the rest of
pawcorder's onboarding code expects.

Public surface
--------------
* ``auto_configure(controller_url, user, password) -> dict`` — log in,
  ensure RTSP is enabled on every channel of every camera, return the
  controller version + a list of cameras with main/sub RTSP URLs.
* ``rtsp_url(host, alias, *, port=7447) -> str`` — convenience builder
  used by tests and any caller that already has an alias in hand.
* ``_is_private_host(controller_url) -> bool`` — used to decide whether
  to disable TLS verification (self-signed certs are the norm on
  RFC1918 / link-local networks; we never bypass cert checks for
  public hostnames).

TLS
---
UniFi OS consoles ship with self-signed certificates. We disable cert
verification only when the controller URL points at an RFC1918 IPv4
address or an IPv6 link-local address. For public hostnames /
addresses we keep verification on so a misconfigured proxy can't
silently MITM credentials.

RTSP
----
Each camera has multiple ``channels`` (high / medium / low). RTSP is a
per-channel toggle; once enabled the controller stamps an
``rtsp_alias`` on the channel and the stream URL is then
``rtsp://{controller_host}:7447/{rtsp_alias}`` with no in-URL
credentials (the alias itself is the secret).

uiprotect's :class:`CameraChannel` doesn't ship a ``set_rtsp_enabled``
helper — it only exposes the field. We mutate the field through
``Camera.queue_update`` (the pattern uiprotect uses for every other
setter on the device), which generates the correct PATCH against the
controller. The helper lives on the camera object as a small
monkey-patch-free closure so the test surface stays simple.
"""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

# uiprotect transitively pulls aiohttp + pydantic + orjson + PyAV (~80MB
# of native deps), all loaded eagerly at admin-process startup. That's
# acceptable for a shipping product surface; if startup-cost ever becomes
# a problem we can move these imports inside auto_configure() — the test
# suite currently patches `ProtectApiClient` at module level so a lazy
# import would require updating the fixtures too.
from uiprotect import ProtectApiClient
from uiprotect.exceptions import (
    ClientError,
    NotAuthorized,
    NvrError,
)


# ---- URL helpers ---------------------------------------------------------

def _is_private_host(controller_url: str) -> bool:
    """True if the controller URL's host is a private IP.

    Accepts RFC1918 IPv4 (10/8, 172.16/12, 192.168/16) and IPv6
    link-local (fe80::/10) / unique-local (fc00::/7). Hostnames are
    NOT considered private — we keep TLS verification on for them so a
    DNS-poisoning attacker can't downgrade us silently.
    """
    host = urlparse(controller_url).hostname or ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private


def _controller_host(controller_url: str) -> str:
    """Extract the bare host (no scheme, no port) from the controller URL."""
    return urlparse(controller_url).hostname or ""


def _controller_port(controller_url: str, default: int = 443) -> int:
    """Return the explicit port from the controller URL, or ``default``."""
    parsed = urlparse(controller_url)
    return parsed.port if parsed.port else default


def rtsp_url(controller_host: str, rtsp_alias: str, *, port: int = 7447) -> str:
    """Build an RTSP URL for a Protect alias.

    Returns an empty string when ``rtsp_alias`` is falsy — callers use
    that to detect "RTSP not yet enabled on this channel".
    """
    if not rtsp_alias:
        return ""
    return f"rtsp://{controller_host}:{port}/{rtsp_alias}"


# ---- RTSP enablement -----------------------------------------------------

async def _ensure_rtsp_enabled(camera: Any) -> None:
    """Make sure every channel on ``camera`` has RTSP enabled.

    If the camera object exposes a ``set_rtsp_enabled(channel_idx, on)``
    coroutine (uiprotect may add one in a future release, and our test
    suite mocks one), we use that. Otherwise we fall back to the
    generic ``queue_update`` pattern uiprotect uses for every other
    setter — flip ``channel.is_rtsp_enabled`` in a callback and let
    uiprotect compute the diff + PATCH.
    """
    channels = getattr(camera, "channels", None) or []
    if not channels:
        return

    explicit = getattr(camera, "set_rtsp_enabled", None)

    for idx, channel in enumerate(channels):
        if getattr(channel, "is_rtsp_enabled", False):
            continue
        if callable(explicit):
            await explicit(idx, True)
            continue

        # Generic uiprotect path: queue an update that flips the flag,
        # let the lib generate + send the PATCH.
        def _callback(ch: Any = channel) -> None:
            ch.is_rtsp_enabled = True

        await camera.queue_update(_callback)


# ---- Output shaping ------------------------------------------------------

def _str_or_empty(value: Any) -> str:
    """Coerce a uiprotect field (Version / IP / None / str) to a plain str."""
    if value is None:
        return ""
    return str(value)


def _shape_camera(camera: Any, controller_host: str) -> dict[str, Any]:
    channels = getattr(camera, "channels", None) or []
    main_alias = getattr(channels[0], "rtsp_alias", "") if len(channels) >= 1 else ""
    sub_alias = getattr(channels[1], "rtsp_alias", "") if len(channels) >= 2 else ""
    return {
        "id": _str_or_empty(getattr(camera, "id", "")),
        "name": _str_or_empty(getattr(camera, "name", "")),
        "mac": _str_or_empty(getattr(camera, "mac", "")),
        "host": _str_or_empty(getattr(camera, "host", "")),
        "model": _str_or_empty(
            getattr(camera, "market_name", None)
            or getattr(camera, "type", None)
            or getattr(camera, "model", None)
            or ""
        ),
        "rtsp_main": rtsp_url(controller_host, main_alias or ""),
        "rtsp_sub": rtsp_url(controller_host, sub_alias or ""),
    }


def _controller_version(client: ProtectApiClient) -> str:
    """Best-effort firmware string from the bootstrap NVR record."""
    try:
        nvr = client.bootstrap.nvr
    except Exception:  # pragma: no cover - defensive, bootstrap not loaded
        return ""
    return _str_or_empty(getattr(nvr, "version", "") or "")


# ---- Public entry point --------------------------------------------------

async def auto_configure(
    controller_url: str,
    user: str,
    password: str,
) -> dict[str, Any]:
    """Log in to the controller, ensure RTSP is on, return cameras + URLs.

    On invalid credentials raises ``PermissionError`` (uiprotect's
    ``NotAuthorized`` is itself a ``PermissionError`` subclass — we
    catch it explicitly so the message is clean). On any other
    network/protocol failure we raise ``RuntimeError`` with a message
    that includes the controller URL.
    """
    host = _controller_host(controller_url)
    port = _controller_port(controller_url)
    verify_ssl = not _is_private_host(controller_url)

    client = ProtectApiClient(
        host=host,
        port=port,
        username=user,
        password=password,
        verify_ssl=verify_ssl,
    )

    try:
        try:
            await client.update()
        except NotAuthorized as exc:
            raise PermissionError("UniFi controller rejected credentials") from exc
        except (ClientError, NvrError) as exc:
            raise RuntimeError(
                f"Could not reach UniFi controller at {controller_url}: {exc}"
            ) from exc
        except OSError as exc:  # connection refused / DNS errors / TLS errors
            raise RuntimeError(
                f"Could not reach UniFi controller at {controller_url}: {exc}"
            ) from exc

        controller_version = _controller_version(client)

        shaped: list[dict[str, Any]] = []
        cameras = list(client.bootstrap.cameras.values())
        for cam in cameras:
            try:
                await _ensure_rtsp_enabled(cam)
            except NotAuthorized as exc:
                raise PermissionError(
                    f"UniFi user lacks permission to enable RTSP on camera {getattr(cam, 'id', '?')}"
                ) from exc
            except (ClientError, NvrError) as exc:
                raise RuntimeError(
                    f"UniFi PATCH failed for camera {getattr(cam, 'id', '?')}: {exc}"
                ) from exc
            shaped.append(_shape_camera(cam, host))
    finally:
        # uiprotect opens an aiohttp session lazily; close it if it was.
        close = getattr(client, "close_session", None)
        if callable(close):
            try:
                await close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

    first = shaped[0] if shaped else {"rtsp_main": "", "rtsp_sub": ""}
    return {
        "device": {
            "controller_version": controller_version,
            "manufacturer": "Ubiquiti UniFi Protect",
        },
        "cameras": shaped,
        # Brand-dispatcher contract keys (top-level, for the first
        # camera). UniFi cameras are PoE only by convention, so the
        # connection_type is hard-coded to "wired".
        "link": None,
        "connection_type": "wired",
        "rtsp_main": first.get("rtsp_main", ""),
        "rtsp_sub": first.get("rtsp_sub", ""),
    }
