"""Espressif ESP32 SoftAP provisioner.

Many ESP32-based whitebrand cameras ship the unmodified
``wifi_provisioning`` IDF component. The provisioner exposes a small
set of HTTP endpoints at ``192.168.4.1`` once the user joins the
device's SoftAP. The protocol uses protobuf-over-HTTP, but in
"unsecured" mode (``security_ver=0``, the default for cheap whitebrand
cams that skip ECDH) it accepts plain JSON-style configuration via
the legacy ``/wifi-config`` route as well.

We support both the modern protobuf endpoint (with ``security_ver=0``,
which most cams ship — security_ver=2 ECDH is rare in the wild) and
the legacy JSON path. Trying the protobuf first matches Espressif's
own provisioning library order.

Reference: ``components/wifi_provisioning`` in ESP-IDF, plus the
Android source of ``ESPProvision`` (Apache-2.0).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct

import httpx

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)
from .softap_join import joined_softap

logger = logging.getLogger("pawcorder.provisioning.softap_espressif")

_DEFAULT_ESP_IP = "192.168.4.1"
_HTTP_TIMEOUT = 8.0


def _encode_protobuf_wifi_config(ssid: str, psk: str) -> bytes:
    """Build the ``WiFiConfigPayload`` protobuf for security_ver=0.

    ESP-IDF's ``wifi_config.proto`` (Apache-2.0) defines the payload as:

        message WiFiConfigPayload {
          uint32 msg = 1;
          oneof payload {
            CmdSetConfig cmd_set_config = 11;
            ...
          }
        }
        message CmdSetConfig {
          bytes ssid = 1;
          bytes passphrase = 2;
          bytes bssid = 3;
          uint32 channel = 4;
        }

    For security_ver=0 we pack this manually. The wire format is small
    enough that pulling in a protobuf compiler dependency would be
    silly — we write the four byte tags by hand.
    """
    def _pb_bytes_field(field_no: int, value: bytes) -> bytes:
        # tag = (field_no << 3) | wire_type(LEN=2)
        tag = (field_no << 3) | 2
        return _pb_varint(tag) + _pb_varint(len(value)) + value

    def _pb_varint(value: int) -> bytes:
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                return bytes(out)

    cmd_set_config = (
        _pb_bytes_field(1, ssid.encode("utf-8"))
        + _pb_bytes_field(2, psk.encode("utf-8"))
    )
    msg_field = _pb_varint((1 << 3) | 0) + _pb_varint(2)  # WiFiConfigMsgType.TypeCmdSetConfig = 2
    cmd_set_config_field = _pb_bytes_field(11, cmd_set_config)
    return msg_field + cmd_set_config_field


async def _post_protobuf_config(
    *, client: httpx.AsyncClient, base_url: str, payload: bytes,
) -> bool:
    try:
        resp = await client.post(
            f"{base_url}/prov-config",
            content=payload,
            headers={"Content-Type": "application/x-protobuf"},
        )
    except (httpx.HTTPError, asyncio.TimeoutError):
        return False
    if resp.status_code >= 400:
        return False
    # Some cameras reply with a CmdRespSetConfig protobuf that has
    # status=0 on success. We treat any 2xx as success because parsing
    # the response would require shipping the wifi_config.proto.
    return True


async def _post_apply(*, client: httpx.AsyncClient, base_url: str) -> None:
    payload = b"\x08\x05\x32\x00"  # apply config message — ApplyConfig=5, empty body
    try:
        await client.post(f"{base_url}/prov-config", content=payload,
                          headers={"Content-Type": "application/x-protobuf"})
    except (httpx.HTTPError, asyncio.TimeoutError):
        pass  # apply is best-effort; cameras often reboot mid-response


async def _post_legacy_json(
    *, client: httpx.AsyncClient, base_url: str,
    ssid: str, psk: str,
) -> bool:
    """Fallback for cameras shipping the older JSON wifi-config route."""
    try:
        resp = await client.post(
            f"{base_url}/wifi-config",
            json={"ssid": ssid, "passwd": psk},
        )
    except (httpx.HTTPError, asyncio.TimeoutError):
        return False
    return resp.status_code < 400


async def push_creds(
    *, softap_ssid: str, softap_ip: str,
    home_ssid: str, home_psk: str,
) -> ProvisionerResult:
    base_url = f"http://{softap_ip or _DEFAULT_ESP_IP}"
    payload = _encode_protobuf_wifi_config(home_ssid, home_psk)
    try:
        async with joined_softap(softap_ssid):
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                ok = await _post_protobuf_config(
                    client=client, base_url=base_url, payload=payload,
                )
                if not ok:
                    ok = await _post_legacy_json(
                        client=client, base_url=base_url,
                        ssid=home_ssid, psk=home_psk,
                    )
                if ok:
                    await _post_apply(client=client, base_url=base_url)
    except RuntimeError as exc:
        return ProvisionerResult(
            ok=False, transport="softap",
            message=f"Could not switch to the camera's setup network: {exc}",
        )
    if not ok:
        return ProvisionerResult(
            ok=False, transport="softap",
            message="ESP32-based camera did not accept the Wi-Fi settings",
        )
    return ProvisionerResult(
        ok=True, transport="softap", needs_arrival_watcher=True,
        message="Sent Wi-Fi settings to the camera. Waiting for it to join…",
    )


class EspressifSoftAPProvisioner(BaseProvisioner):
    transport = "softap"
    capability = "auto"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "espressif-softap"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        d = request.device
        return await push_creds(
            softap_ssid=d.ssid or d.label,
            softap_ip=d.extra.get("softap_ip", "") or _DEFAULT_ESP_IP,
            home_ssid=request.ssid,
            home_psk=request.psk,
        )


class ImouSoftAPProvisioner(BaseProvisioner):
    """Imou SoftAP — detection-only handoff.

    Imou uses a Dahua-derived but cloud-bound provisioning blob. The
    cred push requires an Imou cloud account-linked token we can't mint
    without the Imou developer SDK + paid registration. We still detect
    the SoftAP so the orchestrator can route the user to the Imou Life
    app and the arrival watcher takes over once the camera joins.
    """
    transport = "vendor_app"
    capability = "vendor"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "imou-softap"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        from .base import vendor_app_handoff
        return vendor_app_handoff(
            request.device,
            note=(
                "Imou cameras need the Imou Life app for the first-time "
                "Wi-Fi setup. After it's on the network, Pawcorder will "
                "detect the camera and add it for you."
            ),
        )
