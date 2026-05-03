"""Detection-only BLE provisioner for vendor-locked cameras.

Tapo, Wyze, Eufy, Ring, Nest, Tuya BLE — every one of these cameras
broadcasts a recognisable BLE signature in pairing mode but uses a
proprietary handshake we cannot complete without either:

  * commercial OEM credentials (Tuya cloud token, HomeKit MFi cert), or
  * reverse-engineered protocol bytes that have not been published under
    a license compatible with Pawcorder's permissive OSS posture, or
  * a pairing flow that bakes the user's vendor-cloud account into the
    device — making the cred push useless without that account.

For all of these we still "handle" the device — we tell the user
clearly which vendor app to use, surface the matched MAC for the
``arrival_watcher`` to wait on, and let the orchestrator stitch the
LAN-side handoff back into the standard ``camera_setup`` flow once the
camera actually joins. The user never has to leave Pawcorder's UI to
discover that they need to use the vendor app — the moment they pick
the camera in our scan list, we tell them.

This module is intentionally ~100 lines: there's no protocol to write.
The interesting work is the fingerprint database (``fingerprints.py``)
and the arrival watcher (``arrival_watcher.py``).
"""
from __future__ import annotations

import logging

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
    vendor_app_handoff,
)
from .fingerprints import by_id

logger = logging.getLogger("pawcorder.provisioning.ble_proprietary")


# ---------------------------------------------------------------------------
# Vendor-app message map
# ---------------------------------------------------------------------------

# Each entry tells the user (in plain English; the i18n layer translates
# the wrapping copy) WHICH app to install + WHY this path is needed.
# Phrasing avoids engineering jargon per the user-facing-copy memory.
_VENDOR_NOTES: dict[str, str] = {
    "tapo-ble": (
        "Tapo cameras use TP-Link's own pairing process that only the "
        "Tapo app can complete. After you finish setup in the Tapo app, "
        "Pawcorder will spot the camera on your Wi-Fi and add it for you."
    ),
    "wyze-ble": (
        "Wyze cameras only finish setup through the Wyze app. After you "
        "do that, come back to Pawcorder — we'll have already noticed the "
        "camera and added it."
    ),
    "eufy-ble": (
        "Eufy / Anker cameras pair through the Eufy Security app. Once "
        "the camera is on your Wi-Fi, Pawcorder will pick it up and add it."
    ),
    "reolink-ble": (
        "Reolink battery cameras like Argus need the Reolink app for "
        "first-time pairing. After it's joined Wi-Fi, we'll add it for you."
    ),
    "ring-ble": (
        "Ring cameras pair through the Ring app — Pawcorder can't talk to "
        "them directly until they're on Wi-Fi. After Ring's setup finishes, "
        "we'll see the camera and add it."
    ),
    "nest-ble": (
        "Google Nest cameras pair through Google Home. Once they're on "
        "your Wi-Fi, Pawcorder can pick them up."
    ),
    "tuya-ble": (
        "Smart Life / Tuya cameras pair through the Smart Life app. Once "
        "joined to your Wi-Fi, Pawcorder will detect and add them."
    ),
    "imou-softap": (
        "Imou cameras pair through the Imou Life app. After setup, we'll "
        "find the camera on your Wi-Fi automatically."
    ),
}


class ProprietaryVendorProvisioner(BaseProvisioner):
    """One class for every Class-B vendor — they all return the same handoff.

    The orchestrator queries ``handles()`` per discovered device. Any
    device whose fingerprint has ``capability == "vendor"`` lands here.
    """

    transport = "vendor_app"
    capability = "vendor"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.capability == "vendor"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        device = request.device
        fp = by_id(device.fingerprint_id)
        note = _VENDOR_NOTES.get(device.fingerprint_id, "")
        # Pick whichever app URL we have. Both iOS and Android are stored
        # so the admin UI can render the right deep-link button per the
        # client's User-Agent — but at this layer we just hand back any
        # URL we know about so the orchestrator emits something useful.
        url = ""
        if fp is not None:
            url = (
                fp.metadata.get("vendor_app_ios", "")
                or fp.metadata.get("vendor_app_android", "")
            )
        return vendor_app_handoff(device, vendor_app_url=url, note=note)
