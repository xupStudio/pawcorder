"""Matter (CSA) BLE commissioning provisioner.

Matter cameras advertise BLE service UUID ``0xFFF6`` while in
commissioning mode. The standard commissioning flow is:

  1. Connect over BLE.
  2. Run the BTP (Bluetooth Transport Protocol) handshake.
  3. Establish a PASE session via Spake2+ keyed by the 11-digit
     setup code on the camera's sticker / box.
  4. Send Wi-Fi creds via the ``NetworkCommissioning`` cluster.
  5. Wait for the device to switch from BLE-only to operational on the
     Wi-Fi network.

The full flow is implemented in ``connectedhomeip`` (Apache-2.0) and
exposed by ``python-matter-server`` (Apache-2.0). Both pull large
native dependencies — ``chip-clusters`` ships per-platform wheels with
``libchip`` baked in, and ``python-matter-server`` runs as a separate
process that the admin would IPC with via WebSocket.

Pawcorder treats Matter as a first-class commissioning path when an
external commissioner is available on the host (``chip-tool``,
``python-matter-server``, or a user-provided one): we shell out to it
with the user's setup code + SSID + PSK and let it drive the heavy
lifting. When no commissioner is detected, we fall back to a vendor-app
handoff (Apple Home / Google Home / Samsung SmartThings — any Matter
controller can finish the job).

This split keeps the OSS distribution of Pawcorder lean (no required
Matter dependency) while still automating the case where the user has
one of the standard commissioners on the box.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Optional

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)

logger = logging.getLogger("pawcorder.provisioning.ble_matter")


def _find_chip_tool() -> Optional[str]:
    """Return the path to ``chip-tool`` if available on PATH.

    The CHIP team builds ``chip-tool`` for Linux and macOS as a
    reference commissioner. Pawcorder users who installed
    ``connectedhomeip`` from source (or apt on Debian-based systems
    where the package exists) get end-to-end Matter commissioning for
    free; everyone else falls back to the handoff path.
    """
    return shutil.which("chip-tool")


async def _run_chip_tool(
    *,
    setup_code: str,
    ssid: str,
    psk: str,
    node_id: int,
    timeout_s: float,
) -> tuple[bool, str]:
    """Drive ``chip-tool pairing ble-wifi`` and capture stdout/stderr.

    Returns (ok, message). ``chip-tool`` exits 0 on success and emits
    ``CHIP:CTL: Commissioning complete`` on the last log line. We don't
    parse — we trust the exit code, with the tail of the log captured
    for the UI in the failure path.
    """
    chip_tool = _find_chip_tool()
    if chip_tool is None:
        return False, "chip-tool not installed"

    cmd = [
        chip_tool, "pairing", "ble-wifi",
        str(node_id),
        ssid, psk,
        setup_code, "0",  # discriminator 0 = match any in BLE scan window
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "chip-tool timed out — check the camera is in commissioning mode"

    if proc.returncode == 0:
        return True, "Matter commissioning complete"
    tail = stdout.decode("utf-8", errors="ignore").splitlines()[-12:]
    return False, "chip-tool failed:\n" + "\n".join(tail)


class MatterProvisioner(BaseProvisioner):
    transport = "matter"
    capability = "auto"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "matter-generic"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        device = request.device
        setup_code = request.device.extra.get("matter_setup_code", "")
        if not setup_code:
            # The orchestrator gathers the 11-digit code in a separate
            # UI step before calling provision(); if it's missing we
            # surface a clean failure rather than running chip-tool with
            # an empty argument.
            return ProvisionerResult(
                ok=False,
                transport="matter",
                mac=device.mac,
                message="Matter setup code (11-digit code on the camera) is required",
            )

        if _find_chip_tool() is None:
            # No commissioner on this host — graceful degrade to handoff.
            # The user can still finish via Apple Home / Google Home /
            # SmartThings. ``arrival_watcher`` catches the camera once it
            # joins Wi-Fi.
            return ProvisionerResult(
                ok=True,
                transport="matter",
                mac=device.mac,
                needs_arrival_watcher=True,
                message=(
                    "This camera supports Matter. Use Apple Home, Google "
                    "Home, or SmartThings to add it with the 11-digit "
                    "setup code. Pawcorder will spot it on your Wi-Fi "
                    "afterwards."
                ),
            )

        ok, message = await _run_chip_tool(
            setup_code=setup_code,
            ssid=request.ssid,
            psk=request.psk,
            # Pawcorder allocates Matter node ids per camera, starting at
            # an arbitrary high prefix so we don't collide with any
            # other commissioner the user might also be running.
            node_id=int(request.device.extra.get("matter_node_id", 0x504F4001)),
            timeout_s=120.0,
        )
        return ProvisionerResult(
            ok=ok,
            transport="matter",
            mac=device.mac,
            needs_arrival_watcher=ok,  # only watch if cred push succeeded
            message=message,
        )
