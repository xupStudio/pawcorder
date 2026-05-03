"""Generic Wi-Fi QR provisioner.

The standard ``WIFI:`` URI scheme used by Android's "Share Wi-Fi" QR
codes is recognised by a small but real subset of IP cameras —
Hikvision via the Hik-Connect setup flow, Aqara HomeKit cameras, and
Pawcorder's own test fixtures. The format is::

    WIFI:S:<ssid>;T:<auth>;P:<psk>;H:<hidden>;;

We render an SVG inline so the admin web UI can display it on screen
without an extra fetch — important for SSE flows where the QR appears
mid-stream.

Reference: ZXing wiki, "Wi-Fi configuration via QR code".
"""
from __future__ import annotations

import io
import logging

import qrcode
import qrcode.image.svg

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)

logger = logging.getLogger("pawcorder.provisioning.qr_generic")


_AUTH_TO_QR_FIELD = {
    "open": "nopass",
    "wpa2-psk": "WPA",
    "wpa3-sae": "WPA",
    "wpa3-personal": "WPA",
    "wpa-eap": "WPA",
}


def _escape_qr_field(value: str) -> str:
    """Escape per the WIFI: scheme — backslash-escape ``;``, ``,``, ``"``, ``:``, ``\\``."""
    out: list[str] = []
    for ch in value:
        if ch in (";", ",", '"', ":", "\\"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def build_payload(*, ssid: str, psk: str, auth: str = "wpa2-psk", hidden: bool = False) -> str:
    qr_auth = _AUTH_TO_QR_FIELD.get(auth, "WPA")
    parts = [
        f"S:{_escape_qr_field(ssid)}",
        f"T:{qr_auth}",
    ]
    if qr_auth != "nopass" and psk:
        parts.append(f"P:{_escape_qr_field(psk)}")
    if hidden:
        parts.append("H:true")
    return "WIFI:" + ";".join(parts) + ";;"


def render_svg(payload: str) -> str:
    """Render ``payload`` as an inline-friendly SVG string.

    We use ``qrcode.image.svg.SvgPathImage`` because it produces a
    single ``<path>`` element that scales cleanly and embeds without
    a wrapper ``<image>`` tag — the admin UI just drops it inside a
    div.
    """
    img = qrcode.make(
        payload,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
        border=2,
    )
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


class GenericWifiQRProvisioner(BaseProvisioner):
    transport = "qr"
    capability = "qr"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "generic-qr"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        payload = build_payload(
            ssid=request.ssid,
            psk=request.psk,
            auth=request.auth,
        )
        return ProvisionerResult(
            ok=True,
            transport="qr",
            needs_arrival_watcher=True,
            message=(
                "Hold the QR code in front of the camera lens, about 15 cm "
                "away. The camera will beep when it reads the code; "
                "Pawcorder will pick it up once it joins your Wi-Fi."
            ),
            image_svg=render_svg(payload),
            image_payload=payload,
        )
