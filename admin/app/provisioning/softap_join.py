"""Join / leave a camera's SoftAP from the host.

The vendor-specific SoftAP provisioners (``softap_foscam.py`` etc.) all
need to talk HTTP to the camera's setup endpoint at ``192.168.x.1``,
which means the host must be temporarily associated with the camera's
SoftAP. After the cred push, we have to come back to the home Wi-Fi or
the user loses their admin connection.

This module abstracts that "hop, do thing, hop back" dance behind an
async context manager so per-vendor code is straightforward::

    async with joined_softap("Foscam_AABBCCDD"):
        await foscam_push_creds(...)

We support nmcli (Linux/WSL2 NetworkManager), wpa_supplicant
(``wpa_cli`` for Linux without NM), and macOS ``networksetup``. Windows
without NM support degrades gracefully — the orchestrator detects
unsupported hosts up front and tells the user to run an NM-capable OS
or add the camera with the vendor app.

The "remember the original Wi-Fi and reconnect" path is critical. If
the user's admin session was over Wi-Fi (admin running on a laptop),
disconnecting drops the session — we save the original SSID before we
hop and reconnect on the way out, in a ``finally`` so an exception
during cred push still restores connectivity.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger("pawcorder.provisioning.softap_join")


# ---------------------------------------------------------------------------
# Backend probe
# ---------------------------------------------------------------------------


def _which_join_tool() -> str:
    for tool in ("nmcli", "networksetup", "wpa_cli"):
        if shutil.which(tool):
            return tool
    return ""


def softap_join_available() -> bool:
    return bool(_which_join_tool())


# ---------------------------------------------------------------------------
# nmcli backend
# ---------------------------------------------------------------------------


async def _nmcli_active_ssid() -> str:
    """Return the SSID we're currently connected to, "" if none."""
    proc = await asyncio.create_subprocess_exec(
        "nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    for raw in out.decode("utf-8", errors="ignore").splitlines():
        line = raw.replace(r"\:", "\x00")
        active, _, ssid = line.partition(":")
        if active.lower() == "yes":
            return ssid.replace("\x00", ":")
    return ""


async def _nmcli_connect(ssid: str, *, password: str = "") -> None:
    args = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"nmcli could not connect to {ssid!r}: "
            + err.decode("utf-8", errors="ignore").strip()
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@asynccontextmanager
async def joined_softap(ssid: str) -> AsyncIterator[None]:
    """Async-context-manage a SoftAP join + auto-reconnect.

    On exit, even if the inner block raised, we try to put the host
    back on whatever SSID we were on at entry. We don't store the
    user's home Wi-Fi password — nmcli already remembers it from when
    the user first set it up (the connection profile lives in
    ``/etc/NetworkManager/system-connections/``).
    """
    tool = _which_join_tool()
    if tool != "nmcli":
        # macOS networksetup and wpa_cli paths are technically supported
        # by the same flow, but each has subtle wrinkles around prompting
        # for sudo / Keychain that vary by OS version. We surface a
        # single clean error rather than silently doing nothing.
        raise RuntimeError(
            f"SoftAP join not supported on this host (no nmcli). Use the "
            f"vendor's mobile app to put the camera on Wi-Fi; Pawcorder "
            f"will detect it once it's there."
        )

    original = await _nmcli_active_ssid()
    if original == ssid:
        # Already on the SoftAP — likely a reentry / retry. No need to
        # hop.
        yield
        return

    await _nmcli_connect(ssid)
    try:
        # Cameras' SoftAPs don't always serve DHCP instantly. A short
        # settle gives the kernel time to bind the link and assign an
        # IP before the per-vendor HTTP push tries to connect.
        await asyncio.sleep(2.0)
        yield
    finally:
        try:
            if original:
                await _nmcli_connect(original)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "could not reconnect to original Wi-Fi %r after softap hop: %s",
                original, exc,
            )
