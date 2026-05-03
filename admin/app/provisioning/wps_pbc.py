"""WPS Push-Button-Config provisioner.

A small remaining slice of cameras still support WPS — Cisco/Linksys
re-skinned units, some old Foscam models, a few enterprise
ceiling-mount IPs. The user pushes the WPS button on their *home*
router (or the host runs ``wpa_cli wps_pbc`` on a Wi-Fi adapter
configured as an AP), then pushes the WPS button on the camera, and
the two negotiate a WPA2 key.

Pawcorder's role here is small: trigger ``wpa_cli wps_pbc`` on the
host's wireless interface so users with a Linux host that runs as an
AP don't have to drop to a terminal. We don't implement a Windows /
macOS path — neither shell exposes WPS controls programmatically.

Reference: ``wpa_supplicant`` README-WPS, ``wpa_cli wps_pbc`` command.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)

logger = logging.getLogger("pawcorder.provisioning.wps_pbc")


def wps_available() -> bool:
    """True if ``wpa_cli`` exists on PATH.

    The orchestrator hides the WPS option entirely on hosts that don't
    have it — there's no point offering a button that can't do anything.
    """
    return shutil.which("wpa_cli") is not None


async def _wpa_cli(*args: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        "wpa_cli", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        return False, err.decode("utf-8", errors="ignore").strip()
    return True, out.decode("utf-8", errors="ignore").strip()


async def trigger_pbc() -> ProvisionerResult:
    if not wps_available():
        return ProvisionerResult(
            ok=False, transport="wps", needs_arrival_watcher=False,
            message="WPS push-button is not supported on this host",
        )
    ok, out = await _wpa_cli("wps_pbc")
    if not ok:
        return ProvisionerResult(
            ok=False, transport="wps", needs_arrival_watcher=False,
            message=f"wpa_cli wps_pbc failed: {out}",
        )
    return ProvisionerResult(
        ok=True, transport="wps", needs_arrival_watcher=True,
        message=(
            "WPS push-button window opened for two minutes. Press the "
            "WPS button on your camera now. Pawcorder will pick it up "
            "as soon as it joins your Wi-Fi."
        ),
    )


class WPSProvisioner(BaseProvisioner):
    transport = "wps"
    capability = "broadcast"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.transport == "wps"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        return await trigger_pbc()
