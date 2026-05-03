"""Reolink XML-tagged QR provisioner.

Reolink's setup-by-QR flow expects a QR code whose decoded text is a
small XML document::

    <QR>
      <S>your-ssid</S>
      <P>your-password</P>
    </QR>

The camera plays a confirmation chime when it reads the code, then
joins the Wi-Fi within ~15 seconds. Reolink's support pages and
community confirm this format for the Argus and E1 series.

We deliberately keep the encoder small — there's no protobuf, no
encryption, no per-camera nonce. The camera authenticates the SSID/PSK
on the AP side; the QR is purely a transport.
"""
from __future__ import annotations

import logging

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)
from .qr_generic import render_svg

logger = logging.getLogger("pawcorder.provisioning.qr_reolink")


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_payload(*, ssid: str, psk: str) -> str:
    return (
        "<QR>"
        f"<S>{_xml_escape(ssid)}</S>"
        f"<P>{_xml_escape(psk)}</P>"
        "</QR>"
    )


class ReolinkQRProvisioner(BaseProvisioner):
    transport = "qr"
    capability = "qr"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "reolink-qr"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        payload = build_payload(ssid=request.ssid, psk=request.psk)
        return ProvisionerResult(
            ok=True,
            transport="qr",
            needs_arrival_watcher=True,
            message=(
                "Hold the QR code about 30 cm in front of the Reolink "
                "camera. It will play a chime when it reads the code. "
                "Pawcorder will pick it up as soon as it joins your Wi-Fi."
            ),
            image_svg=render_svg(payload),
            image_payload=payload,
        )
