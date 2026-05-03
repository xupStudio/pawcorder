"""HNAP (D-Link / older TP-Link) SoftAP provisioner.

HNAP is a SOAP-over-HTTP RPC mechanism D-Link defined and many older
white-label cams (including legacy TP-Link before they switched to
Tapo's proprietary stack) implement. The Wi-Fi config call is
``SetWLanRadioSecurity``.

Reference: HNAP1 documentation (originally Pure Networks, then D-Link),
plus the open-source ``pyW215`` smart-plug library that exposes the
auth scheme. We don't depend on ``pyW215`` because it's specialised
for plugs — we use httpx directly with HMAC-SHA256 for the
``HNAP_AUTH`` header it requires.

This provisioner is best-effort. The HNAP wire format varies subtly
between vendors and firmware versions; if the SOAP response is not
``OK``, we return a clear error and let the user fall back to the
vendor app.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import xml.etree.ElementTree as ET

import httpx

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)
from .softap_join import joined_softap

logger = logging.getLogger("pawcorder.provisioning.softap_hnap")

_DEFAULT_HNAP_IP = "192.168.0.1"
_HTTP_TIMEOUT = 10.0
_DEFAULT_CREDS = (("admin", "admin"), ("admin", ""), ("admin", "12345"))


def _hnap_login_request(user: str, password: str, challenge: str, public_key: str) -> str:
    """Compute the HMAC-SHA256-based HNAP login token."""
    private_key = hmac.new(
        (public_key + password).encode("utf-8"),
        challenge.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()
    login_pwd = hmac.new(
        private_key.encode("utf-8"),
        challenge.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()
    return private_key, login_pwd


def _build_soap_envelope(action: str, params: dict) -> str:
    body_parts = []
    for k, v in params.items():
        body_parts.append(f"<{k}>{v}</{k}>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        f'<{action} xmlns="http://purenetworks.com/HNAP1/">'
        f'{"".join(body_parts)}'
        f'</{action}>'
        '</soap:Body>'
        '</soap:Envelope>'
    )


def _hnap_auth_header(action: str, private_key: str) -> tuple[str, str]:
    timestamp = str(int(time.time() * 1000))
    auth = hmac.new(
        private_key.encode("utf-8"),
        f'{timestamp}"http://purenetworks.com/HNAP1/{action}"'.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()
    return auth, timestamp


async def _hnap_call(
    *, client: httpx.AsyncClient, base_url: str,
    action: str, params: dict, private_key: str = "",
) -> ET.Element:
    body = _build_soap_envelope(action, params)
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPACTION": f'"http://purenetworks.com/HNAP1/{action}"',
    }
    if private_key:
        auth, ts = _hnap_auth_header(action, private_key)
        headers["HNAP_AUTH"] = f"{auth} {ts}"
    resp = await client.post(f"{base_url}/HNAP1/", content=body, headers=headers)
    resp.raise_for_status()
    return ET.fromstring(resp.text)


async def _try_login(
    *, client: httpx.AsyncClient, base_url: str, user: str, password: str,
) -> tuple[bool, str]:
    """Returns (ok, private_key). Empty private_key on failure."""
    challenge_root = await _hnap_call(
        client=client, base_url=base_url, action="Login",
        params={"Action": "request", "Username": user, "LoginPassword": "",
                "Captcha": ""},
    )
    ns = "{http://purenetworks.com/HNAP1/}"
    challenge_resp = challenge_root.find(f".//{ns}LoginResponse")
    if challenge_resp is None:
        return False, ""
    challenge = (challenge_resp.findtext(f"{ns}Challenge") or "").strip()
    public_key = (challenge_resp.findtext(f"{ns}PublicKey") or "").strip()
    cookie = (challenge_resp.findtext(f"{ns}Cookie") or "").strip()
    if not (challenge and public_key):
        return False, ""

    private_key, login_pwd = _hnap_login_request(user, password, challenge, public_key)
    client.cookies.set("uid", cookie)
    client.cookies.set("PrivateKey", private_key)

    final_root = await _hnap_call(
        client=client, base_url=base_url, action="Login",
        params={"Action": "login", "Username": user,
                "LoginPassword": login_pwd, "Captcha": ""},
        private_key=private_key,
    )
    result = (final_root.findtext(f".//{ns}LoginResult") or "").strip().upper()
    return (result == "SUCCESS"), (private_key if result == "SUCCESS" else "")


def _hnap_security_token(home_auth: str) -> tuple[str, str]:
    """Map our auth label to (Type, Encryption)."""
    a = (home_auth or "").lower()
    if a == "open":
        return "None", "None"
    if a == "wpa3-sae":
        return "WPA3-PSK", "AES"
    return "WPA2-PSK", "AES"


async def push_creds(
    *, softap_ssid: str, softap_ip: str,
    home_ssid: str, home_psk: str, home_auth: str,
) -> ProvisionerResult:
    base_url = f"http://{softap_ip or _DEFAULT_HNAP_IP}"
    sec_type, sec_enc = _hnap_security_token(home_auth)
    try:
        async with joined_softap(softap_ssid):
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                pk = ""
                last_err = "no default credentials accepted"
                for user, password in _DEFAULT_CREDS:
                    try:
                        ok, pk = await _try_login(
                            client=client, base_url=base_url,
                            user=user, password=password,
                        )
                    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                        return ProvisionerResult(
                            ok=False, transport="softap",
                            message=f"camera unreachable: {exc}",
                        )
                    if ok:
                        break
                if not pk:
                    return ProvisionerResult(
                        ok=False, transport="softap", message=last_err,
                    )
                # SetWLanRadioSecurity is the documented HNAP action for
                # writing the radio's PSK / type / encryption.
                resp_root = await _hnap_call(
                    client=client, base_url=base_url,
                    action="SetWLanRadioSecurity",
                    params={
                        "RadioID": "RADIO_2.4GHz_1",
                        "Enabled": "true",
                        "Type": sec_type,
                        "Encryption": sec_enc,
                        "Key": home_psk,
                        "SSID": home_ssid,
                    },
                    private_key=pk,
                )
                ns = "{http://purenetworks.com/HNAP1/}"
                result = (resp_root.findtext(
                    f".//{ns}SetWLanRadioSecurityResult"
                ) or "").upper()
                if result != "OK":
                    return ProvisionerResult(
                        ok=False, transport="softap",
                        message="camera rejected the Wi-Fi settings",
                    )
                # SetWLanRadioRestart applies the new config without
                # waiting for a full reboot — D-Link doc-recommended.
                await _hnap_call(
                    client=client, base_url=base_url,
                    action="SetWLanRadioRestart",
                    params={"RadioID": "RADIO_2.4GHz_1"},
                    private_key=pk,
                )
    except RuntimeError as exc:
        return ProvisionerResult(
            ok=False, transport="softap",
            message=f"Could not switch to the camera's setup network: {exc}",
        )
    return ProvisionerResult(
        ok=True, transport="softap", needs_arrival_watcher=True,
        message="Sent Wi-Fi settings via HNAP. Waiting for the camera to join…",
    )


class HNAPSoftAPProvisioner(BaseProvisioner):
    transport = "softap"
    capability = "auto"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.fingerprint_id == "dlink-hnap-softap"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        d = request.device
        return await push_creds(
            softap_ssid=d.ssid or d.label,
            softap_ip=d.extra.get("softap_ip", "") or _DEFAULT_HNAP_IP,
            home_ssid=request.ssid,
            home_psk=request.psk,
            home_auth=request.auth,
        )
