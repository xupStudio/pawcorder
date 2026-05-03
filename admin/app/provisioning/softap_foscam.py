"""Foscam SoftAP provisioner.

When a Foscam camera in setup mode is joined, it answers HTTP at
``http://192.168.0.1`` with the standard Foscam CGI surface
(``/cgi-bin/CGIProxy.fcgi``). We hit ``setWifiSetting`` with the user's
SSID + PSK + auth, then ``rebootSystem`` to make the camera drop the
SoftAP and try the new credentials. The arrival watcher catches the
camera on its first DHCP lease.

Reference: Foscam HD IP Camera CGI User Guide, ``setWifiSetting``
section. We replicate the wire format directly so we don't need the
LGPL ``foscam-python-lib`` (the unmaintained MIT alternative
``libpyfoscam`` does the same thing under a permissive license but
adds connection bookkeeping we don't need for a one-shot push).

Foscam's WPA flag is an integer:

  ====  ====================
   0    Open (no encryption)
   1    WEP   (deprecated)
   2    WPA-PSK
   3    WPA2-PSK
   4    WPA/WPA2 mixed
  ====  ====================

We map ``"open"`` → 0, anything else with a PSK → 4 (mixed mode), which
is the most permissive setting and the one Foscam's own iOS app uses
when adding a camera to a new network.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse

import httpx

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)
from .softap_join import joined_softap

logger = logging.getLogger("pawcorder.provisioning.softap_foscam")

_DEFAULT_FOSCAM_IP = "192.168.0.1"
_FOSCAM_DEFAULT_USER = "admin"
_FOSCAM_DEFAULT_PWD = ""        # Foscam factory password is empty.
_HTTP_TIMEOUT = 8.0


def _foscam_auth_id(auth: str) -> int:
    if auth == "open":
        return 0
    return 4  # WPA/WPA2 mixed PSK — superset of wpa2-psk and wpa3-sae fallback


async def _foscam_cgi(
    *,
    base_url: str,
    cmd: str,
    params: dict,
    user: str,
    password: str,
) -> str:
    """Call one Foscam CGI command. Returns raw response body."""
    qs = urllib.parse.urlencode(
        {"cmd": cmd, "usr": user, "pwd": password, **params},
        quote_via=urllib.parse.quote,
    )
    url = f"{base_url}/cgi-bin/CGIProxy.fcgi?{qs}"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def push_creds(
    *,
    softap_ssid: str,
    softap_ip: str,
    home_ssid: str,
    home_psk: str,
    home_auth: str,
    user: str = _FOSCAM_DEFAULT_USER,
    password: str = _FOSCAM_DEFAULT_PWD,
) -> ProvisionerResult:
    """Single-shot: hop to the SoftAP, push creds, reboot."""
    base_url = f"http://{softap_ip or _DEFAULT_FOSCAM_IP}"
    auth_id = _foscam_auth_id(home_auth)
    try:
        async with joined_softap(softap_ssid):
            # 1) Push Wi-Fi config. ``isEnable=1`` enables the radio.
            await _foscam_cgi(
                base_url=base_url,
                cmd="setWifiSetting",
                user=user,
                password=password,
                params={
                    "isEnable": 1,
                    "isUseWifi": 1,
                    "ssid": home_ssid,
                    "netType": 0,         # 0 = infrastructure (managed AP)
                    "encryptType": auth_id,
                    "psk": home_psk,
                    "authMode": 0,        # 0 = open auth (PSK is the secret)
                    "keyFormat": 1,       # 1 = ASCII
                    "defaultKey": 1,
                    "key1": "",
                    "key2": "",
                    "key3": "",
                    "key4": "",
                    "key1Len": 64,
                    "key2Len": 64,
                    "key3Len": 64,
                    "key4Len": 64,
                },
            )
            # 2) Trigger reboot so the camera drops SoftAP and joins.
            await _foscam_cgi(
                base_url=base_url,
                cmd="rebootSystem",
                user=user,
                password=password,
                params={},
            )
    except RuntimeError as exc:
        return ProvisionerResult(
            ok=False, transport="softap",
            message=f"Could not switch to the camera's setup network: {exc}",
        )
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        return ProvisionerResult(
            ok=False, transport="softap",
            message=f"Foscam camera did not accept the Wi-Fi settings: {exc}",
        )
    return ProvisionerResult(
        ok=True,
        transport="softap",
        needs_arrival_watcher=True,
        message="Sent Wi-Fi settings to the Foscam camera. Waiting for it to join the network…",
    )


class FoscamSoftAPProvisioner(BaseProvisioner):
    transport = "softap"
    capability = "auto"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "foscam-softap"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        d = request.device
        return await push_creds(
            softap_ssid=d.ssid or d.label,
            softap_ip=d.extra.get("softap_ip", "") or _DEFAULT_FOSCAM_IP,
            home_ssid=request.ssid,
            home_psk=request.psk,
            home_auth=request.auth,
        )
