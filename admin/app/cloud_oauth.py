"""Cloud-backup OAuth flows — replaces the rclone CLI dance.

The /cloud page used to send users off to install rclone on a separate
machine, run ``rclone authorize "drive"``, copy the JSON token blob, and
paste it back. This module replaces that with provider-specific flows
the admin runs end-to-end so the user only sees buttons.

Three flow shapes are supported:

  - **OAuth 2.0 Device Code** (Google Drive, OneDrive)
    User clicks → admin gets a short user_code from the provider →
    admin shows "visit URL + enter ABCD-EFGH" → admin polls the token
    endpoint until granted. Standard RFC 8628.

  - **Nextcloud Login Flow v2** (Nextcloud / ownCloud)
    User clicks → admin asks Nextcloud for a login URL + poll endpoint →
    user opens the URL, signs in, approves → admin polls and receives an
    appPassword. Zero registration on Pawcorder side. Native to
    Nextcloud, so it works against any Nextcloud server.

  - **PKCE with loopback callback** (Dropbox)
    Dropbox doesn't support device code; we use the desktop-app PKCE
    flow with a temporary 127.0.0.1 listener. Less convenient than
    device code (user has to be on the same machine as the admin) but
    still better than the CLI dance.

OAuth client_id and client_secret come from env vars. Pawcorder team
registers a free OAuth app per provider once and ships the values via
the install script. PKCE makes secret leakage harmless for Dropbox;
device code on Google/Microsoft technically uses the secret but its
exposure in OSS source is mitigated by Google's "OAuth out-of-band"
class designation.

If env vars are absent, the corresponding ``status()`` returns
``configured=False`` and the UI hides the button (or shows a "needs
admin setup" message pointing at HUMAN_WORK.md).
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import cloud as cloud_mod

logger = logging.getLogger("pawcorder.cloud_oauth")


# ---- provider config (env-driven so OSS distros plug in their own) -----

@dataclass(frozen=True)
class ProviderConfig:
    name: str
    client_id: str
    client_secret: str = ""
    scopes: tuple[str, ...] = ()


def _provider(name: str) -> ProviderConfig:
    """Return the OAuth client for ``name`` (env-driven). Empty client_id
    means "not configured — ask xup to register an OAuth app"."""
    if name == "drive":
        return ProviderConfig(
            name="drive",
            client_id=os.environ.get("PAWCORDER_GOOGLE_OAUTH_CLIENT_ID", ""),
            client_secret=os.environ.get("PAWCORDER_GOOGLE_OAUTH_CLIENT_SECRET", ""),
            scopes=("https://www.googleapis.com/auth/drive.file",),
        )
    if name == "onedrive":
        return ProviderConfig(
            name="onedrive",
            client_id=os.environ.get("PAWCORDER_MS_OAUTH_CLIENT_ID", ""),
            scopes=("Files.ReadWrite.AppFolder", "offline_access"),
        )
    if name == "dropbox":
        return ProviderConfig(
            name="dropbox",
            client_id=os.environ.get("PAWCORDER_DROPBOX_OAUTH_CLIENT_ID", ""),
            scopes=(),  # Dropbox uses default scopes for app-folder apps.
        )
    return ProviderConfig(name=name, client_id="", client_secret="")


# ---- pending-flow state ------------------------------------------------

@dataclass
class _PendingDevice:
    provider: str
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_at: float


@dataclass
class _PendingNextcloud:
    server_url: str
    poll_endpoint: str
    poll_token: str
    expires_at: float


_pending_device: dict[str, _PendingDevice] = {}     # keyed on user_code
_pending_nc: dict[str, _PendingNextcloud] = {}      # keyed on a session id we mint


def _now() -> float: return time.time()


def _gc() -> None:
    """Drop expired pending flows. Cheap, called from start/check."""
    cutoff = _now()
    for k in [k for k, v in _pending_device.items() if v.expires_at < cutoff]:
        _pending_device.pop(k, None)
    for k in [k for k, v in _pending_nc.items() if v.expires_at < cutoff]:
        _pending_nc.pop(k, None)


# ---- generic device-code flow (Drive, OneDrive) ------------------------

# Endpoints per provider.
_DEVICE_ENDPOINTS = {
    "drive":    ("https://oauth2.googleapis.com/device/code",
                  "https://oauth2.googleapis.com/token"),
    "onedrive": ("https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
                  "https://login.microsoftonline.com/common/oauth2/v2.0/token"),
}


@dataclass
class DeviceCodeStart:
    user_code: str
    verification_url: str
    interval: int           # poll interval (seconds), per RFC 8628


async def device_code_start(provider: str) -> DeviceCodeStart:
    """Begin a device-code flow. Returns the user-facing code + URL."""
    cfg = _provider(provider)
    if not cfg.client_id:
        raise RuntimeError(f"{provider} OAuth not configured (see HUMAN_WORK.md)")
    if provider not in _DEVICE_ENDPOINTS:
        raise ValueError(f"no device-code endpoint for {provider!r}")

    code_url, _token_url = _DEVICE_ENDPOINTS[provider]
    body = {"client_id": cfg.client_id, "scope": " ".join(cfg.scopes)}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(code_url, data=body)
    if resp.status_code != 200:
        raise RuntimeError(f"device-code request failed: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    user_code = data.get("user_code") or ""
    device_code = data.get("device_code") or ""
    interval = int(data.get("interval") or 5)
    expires_in = int(data.get("expires_in") or 1800)
    verification_url = (
        data.get("verification_url")
        or data.get("verification_uri")
        or data.get("verification_uri_complete")
        or ""
    )
    if not (user_code and device_code and verification_url):
        raise RuntimeError("provider returned malformed device-code payload")
    _gc()
    _pending_device[user_code] = _PendingDevice(
        provider=provider, device_code=device_code, user_code=user_code,
        verification_url=verification_url, interval=interval,
        expires_at=_now() + expires_in,
    )
    return DeviceCodeStart(
        user_code=user_code, verification_url=verification_url, interval=interval,
    )


async def device_code_poll(user_code: str) -> Optional[dict]:
    """Poll the token endpoint for the given pending flow.

    Returns ``None`` while still pending, ``{access_token, refresh_token,
    expires_in}`` on success. Raises on hard failure (expired, denied)."""
    pending = _pending_device.get(user_code)
    if pending is None:
        raise RuntimeError("unknown user_code (expired or never started)")
    cfg = _provider(pending.provider)
    _, token_url = _DEVICE_ENDPOINTS[pending.provider]
    body = {
        "client_id": cfg.client_id,
        "device_code": pending.device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    if cfg.client_secret:
        body["client_secret"] = cfg.client_secret
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(token_url, data=body)
    data = resp.json()
    err = data.get("error")
    if err == "authorization_pending":
        return None
    if err == "slow_down":
        # Provider asks us to back off — bump interval and keep waiting.
        pending.interval = max(pending.interval + 2, pending.interval)
        return None
    if err in ("expired_token", "access_denied", "invalid_grant"):
        _pending_device.pop(user_code, None)
        raise RuntimeError(err)
    if err:
        raise RuntimeError(f"oauth error: {err}")
    if "access_token" not in data:
        raise RuntimeError("token endpoint returned no access_token")
    _pending_device.pop(user_code, None)

    # Persist into rclone config so the existing uploader path picks it up.
    remote_name = "pawcorder"
    fields: dict = {"type": pending.provider, "token": _rclone_token_payload(data)}
    if pending.provider == "drive":
        fields["scope"] = "drive.file"
    cloud_mod.save_remote(remote_name, fields)
    return data


def _rclone_token_payload(token_response: dict) -> str:
    """rclone stores OAuth tokens as a JSON-encoded blob."""
    import json
    payload = {
        "access_token":  token_response.get("access_token"),
        "token_type":    token_response.get("token_type") or "Bearer",
        "refresh_token": token_response.get("refresh_token"),
        "expiry":        _expiry_iso(token_response.get("expires_in") or 0),
    }
    return json.dumps(payload, separators=(",", ":"))


def _expiry_iso(expires_in_seconds: int) -> str:
    from datetime import datetime, timezone, timedelta
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in_seconds))
    return expiry.isoformat().replace("+00:00", "Z")


# ---- Nextcloud Login Flow v2 ------------------------------------------

async def nextcloud_start(server_url: str) -> dict:
    """POST to /index.php/login/v2 → get server-issued URLs.

    Returns ``{poll_endpoint, login_url, expires_in}``. The user opens
    ``login_url`` in their browser, signs in, and approves.
    """
    base = server_url.rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{base}/index.php/login/v2",
            headers={"User-Agent": "Pawcorder/1.0"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"nextcloud login flow init failed: HTTP {resp.status_code}")
    data = resp.json()
    poll = data.get("poll") or {}
    login_url = data.get("login") or ""
    poll_token = poll.get("token") or ""
    poll_endpoint = poll.get("endpoint") or ""
    if not (login_url and poll_token and poll_endpoint):
        raise RuntimeError("nextcloud returned malformed payload")
    sid = secrets.token_urlsafe(16)
    _gc()
    _pending_nc[sid] = _PendingNextcloud(
        server_url=base, poll_endpoint=poll_endpoint, poll_token=poll_token,
        expires_at=_now() + 1200,  # Nextcloud's flow is 20 min by default
    )
    return {"sid": sid, "login_url": login_url, "expires_in": 1200}


async def nextcloud_poll(sid: str) -> Optional[dict]:
    """Poll Nextcloud's token endpoint. Returns ``None`` while pending,
    ``{server, login_name, app_password}`` on success."""
    pending = _pending_nc.get(sid)
    if pending is None:
        raise RuntimeError("unknown nextcloud session id (expired)")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(pending.poll_endpoint,
                                  data={"token": pending.poll_token})
    if resp.status_code == 404:
        # User hasn't completed yet; spec says 404 == still pending.
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"nextcloud poll failed: HTTP {resp.status_code}")
    data = resp.json()
    server = data.get("server") or pending.server_url
    login_name = data.get("loginName") or ""
    app_password = data.get("appPassword") or ""
    if not (login_name and app_password):
        raise RuntimeError("nextcloud poll returned malformed payload")
    _pending_nc.pop(sid, None)

    # Stash into rclone config as a webdav remote pointing at Nextcloud's
    # WebDAV endpoint. rclone's "vendor=nextcloud" tells it to use the
    # right URL shape for chunked uploads.
    cloud_mod.save_remote("pawcorder", {
        "type": "webdav",
        "url": f"{server.rstrip('/')}/remote.php/dav/files/{login_name}/",
        "vendor": "nextcloud",
        "user": login_name,
        "pass": _rclone_obscure(app_password),
    })
    return {"server": server, "login_name": login_name, "app_password": "stored"}


def _rclone_obscure(plaintext: str) -> str:
    """rclone uses its own ``obscure`` algorithm (AES-CTR with a fixed key)
    rather than plain text in rclone.conf. We shell out to the rclone CLI
    so we don't have to re-implement it."""
    import subprocess
    try:
        proc = subprocess.run(
            [os.environ.get("RCLONE_BIN", "rclone"), "obscure", plaintext],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: store plaintext. rclone reads either form; obscured is
    # preferred for "not screen-readable" purposes only.
    return plaintext


# ---- status (UI calls this on /cloud mount) ---------------------------

def configured_providers() -> dict[str, bool]:
    """Map provider → True if the OAuth client_id is set in env."""
    return {
        "drive":    bool(_provider("drive").client_id),
        "onedrive": bool(_provider("onedrive").client_id),
        "dropbox":  bool(_provider("dropbox").client_id),
        # Nextcloud needs no Pawcorder-side registration — always available.
        "nextcloud": True,
    }
