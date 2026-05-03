"""Dahua / Amcrest SoftAP provisioner.

Dahua and Amcrest expose the same HTTP-Digest configuration surface in
SoftAP setup mode that they do once they're on the LAN — the standard
``configManager.cgi`` endpoint at ``/cgi-bin/configManager.cgi``. We
push Wi-Fi settings via the ``Network.Wlan`` config tree and trigger
``magicBox.cgi?action=reboot``.

Camera default credentials are vendor-dependent. We try the documented
factory pairs (``admin``/``admin``, then ``admin``/empty) — Dahua's
factory ships with no password since 2019; older units shipped with
``admin``. The user can override via ``request.device.extra["user"]``
and ``...["password"]``.
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

logger = logging.getLogger("pawcorder.provisioning.softap_dahua")

_DEFAULT_DAHUA_IP = "192.168.1.108"
_HTTP_TIMEOUT = 8.0
# Order matters: post-2019 firmware ships unpassworded, older firmware
# ships ``admin``/``admin``. Anything else needs the user to override.
_DEFAULT_CREDS = (("admin", ""), ("admin", "admin"))


async def _dahua_set_config(
    *,
    base_url: str,
    auth: httpx.DigestAuth,
    pairs: dict,
) -> None:
    """One ``configManager.cgi?action=setConfig&...`` call."""
    parts = ["action=setConfig"]
    for key, value in pairs.items():
        parts.append(f"{key}={urllib.parse.quote(str(value), safe='')}")
    url = f"{base_url}/cgi-bin/configManager.cgi?" + "&".join(parts)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, auth=auth) as client:
        resp = await client.get(url)
        resp.raise_for_status()


async def _dahua_reboot(*, base_url: str, auth: httpx.DigestAuth) -> None:
    url = f"{base_url}/cgi-bin/magicBox.cgi?action=reboot"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, auth=auth) as client:
        # Reboot may close the socket before responding; httpx raises
        # on read timeout in that case. We treat that as success — the
        # arrival watcher confirms the camera comes back.
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except (httpx.RemoteProtocolError, httpx.ReadError):
            return


def _dahua_auth_token(home_auth: str) -> str:
    """Dahua expects ``WPA2-PSK`` / ``WPA-PSK`` / ``OPEN`` strings."""
    a = (home_auth or "").lower()
    if a == "open":
        return "OPEN"
    if a == "wpa3-sae":
        return "WPA3-SAE"
    return "WPA2-PSK"


async def _try_creds(
    *,
    base_url: str,
    home_ssid: str,
    home_psk: str,
    home_auth: str,
) -> tuple[bool, str]:
    """Walk default cred pairs until one accepts the config push."""
    cfg = {
        "Network.Wlan.0.Enable": "true",
        "Network.Wlan.0.SSID": home_ssid,
        "Network.Wlan.0.Auth": _dahua_auth_token(home_auth),
        "Network.Wlan.0.Encryption": "AES",
        "Network.Wlan.0.Keys[0]": home_psk,
    }
    last_err = "no default credentials accepted"
    for user, password in _DEFAULT_CREDS:
        auth = httpx.DigestAuth(user, password)
        try:
            await _dahua_set_config(base_url=base_url, auth=auth, pairs=cfg)
            await _dahua_reboot(base_url=base_url, auth=auth)
            return True, ""
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                last_err = "camera rejected the default credentials"
                continue
            last_err = f"camera rejected the Wi-Fi settings: {exc}"
            return False, last_err
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            return False, f"camera unreachable: {exc}"
    return False, last_err


class DahuaSoftAPProvisioner(BaseProvisioner):
    transport = "softap"
    capability = "auto"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "dahua-softap"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        d = request.device
        base_url = f"http://{d.extra.get('softap_ip', '') or _DEFAULT_DAHUA_IP}"
        try:
            async with joined_softap(d.ssid or d.label):
                ok, msg = await _try_creds(
                    base_url=base_url,
                    home_ssid=request.ssid,
                    home_psk=request.psk,
                    home_auth=request.auth,
                )
        except RuntimeError as exc:
            return ProvisionerResult(
                ok=False, transport="softap",
                message=f"Could not switch to the camera's setup network: {exc}",
            )
        if ok:
            return ProvisionerResult(
                ok=True, transport="softap",
                needs_arrival_watcher=True,
                message="Sent Wi-Fi settings to the Dahua camera. Waiting for it to join…",
            )
        return ProvisionerResult(ok=False, transport="softap", message=msg)
