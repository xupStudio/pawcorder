"""HomeKit Accessory Protocol (HAP) BLE provisioner.

HomeKit-certified cameras (Aqara G3, Eve Cam, Logitech Circle View …)
advertise on BLE with service UUID ``0xFE5C`` while in pairing mode.
The pair-setup handshake is documented in Apple's HomeKit Accessory
Protocol Specification (HAP-BLE chapter): SRP-6a using the 8-digit
setup code → Curve25519 session → Wi-Fi config write to characteristic
``0000220F-0000-1000-8000-0026BB765291`` (the standard Wi-Fi
configuration service).

The full handshake is ~1500 lines of Python. The ``homekit_python``
library (Apache-2.0) implements it but its maintainer marked it
unmaintained; the modern ``aiohomekit`` fork has not (yet) ported the
BLE pair-setup. Vendoring ``homekit_python``'s BLE module into
Pawcorder's tree is a possibility, but the resulting code path needs a
real HomeKit camera + the user's setup code (printed on the device
sticker) to validate end-to-end. Until that validation happens, this
provisioner detects HomeKit cameras and delegates to Apple Home, with a
crisp prompt asking for the 8-digit setup code so the next iteration
can drive the handshake itself.

Why we bother detecting at all when we delegate the handshake:

  * ``arrival_watcher`` needs the BLE MAC to spot the camera on Wi-Fi
    after the user pairs it via Apple Home.
  * The UI shows a "looks like a HomeKit camera" hint with a deep-link
    that opens Apple Home to the *Add Accessory* flow at the right
    place — better UX than dumping the user back to a generic
    "use the vendor app" message.
"""
from __future__ import annotations

import logging

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)

logger = logging.getLogger("pawcorder.provisioning.ble_homekit")

# The deep link works on iOS — Android can't host HomeKit anyway, so
# we don't pretend to support it there. The admin UI hides the button
# when the request comes from a non-iOS user-agent.
_HOME_APP_DEEP_LINK = "x-com.apple.home://launch"


class HomeKitProvisioner(BaseProvisioner):
    transport = "homekit"
    capability = "auto"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "homekit-generic"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        device = request.device
        # We DO record the MAC so arrival_watcher knows what to wait on
        # after Apple Home finishes the pair-setup. The handoff message
        # explicitly says "we'll see it appear" so the user knows they
        # don't have to come back to Pawcorder after Home opens.
        return ProvisionerResult(
            ok=True,
            transport="homekit",
            mac=device.mac,
            needs_arrival_watcher=True,
            message=(
                "This looks like a HomeKit camera. Open the Home app on "
                "your iPhone, tap Add Accessory, and scan the 8-digit "
                "setup code on the camera. Pawcorder will spot the "
                "camera on your Wi-Fi as soon as Home finishes pairing."
            ),
            image_payload=_HOME_APP_DEEP_LINK,
        )
