"""Home Assistant auto-detect + automation push.

Replaces two of the three engineer-work touchpoints on /home-assistant:
  - Hard-coded HA URL paste → auto-detect from common addresses.
  - Copy/paste the automation YAML into HA's configuration.yaml → push
    via HA's REST API after a token is provided.

The remaining unavoidable touchpoint is the long-lived access token. HA
doesn't expose a programmatic "issue token" API to external apps without
an existing access token or admin credentials. Documenting clearly +
providing a deep-link to the user-profile page keeps friction minimal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("pawcorder.ha_integration")

# Common addresses HA listens on. We probe in order; first 200 wins.
# Includes both docker-compose hostnames and localhost variants so this
# works whether HA runs alongside Pawcorder or on the same LAN.
PROBE_URLS = (
    "http://homeassistant:8123",
    "http://homeassistant.local:8123",
    "http://127.0.0.1:8123",
    "http://localhost:8123",
)

# Pawcorder's signature automation. Pushed to /api/config/automation/config/<id>.
# id is a stable slug so re-pushing replaces rather than duplicating.
AUTOMATION_ID = "pawcorder_pet_detected"


@dataclass
class Status:
    reachable: bool
    base_url: str = ""
    version: str = ""
    error: str = ""


async def detect() -> Status:
    """Probe common HA addresses, return the first that responds."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        for url in PROBE_URLS:
            try:
                resp = await client.get(f"{url}/api/")
            except httpx.HTTPError:
                continue
            # /api/ returns 401 unauthenticated when HA is up — that's
            # what we want to detect "HA is here, just need a token".
            if resp.status_code in (200, 401):
                # /api/discovery_info is unauthenticated → tells us the
                # HA version even without a token.
                version = ""
                try:
                    di = await client.get(f"{url}/api/discovery_info")
                    if di.status_code == 200:
                        version = (di.json() or {}).get("version") or ""
                except httpx.HTTPError:
                    pass
                return Status(reachable=True, base_url=url, version=version)
    return Status(reachable=False, error="Home Assistant not found at common addresses")


async def verify_token(base_url: str, token: str) -> tuple[bool, str]:
    """Test that the token authenticates against HA's REST API."""
    if not token:
        return False, "token required"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{base_url}/api/", headers=headers)
        except httpx.HTTPError as exc:
            return False, str(exc)[:200]
    if resp.status_code == 401:
        return False, "token rejected by Home Assistant"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    return True, "ok"


def _build_automation_yaml(notify_target: str = "notify.mobile_app_phone") -> dict:
    """Pawcorder pet-detected automation, in JSON form (HA accepts JSON
    via REST and stores as YAML internally). The MQTT trigger taps Frigate's
    events stream — Pawcorder ships an MQTT broker via Frigate by default."""
    return {
        "id": AUTOMATION_ID,
        "alias": "Pawcorder — pet detected",
        "description": "Created by Pawcorder admin. Pushes a notification when a cat or dog is detected.",
        "trigger": [
            {
                "platform": "mqtt",
                "topic": "frigate/events",
                "value_template": "{{ value_json.type }}",
                "payload": "new",
            }
        ],
        "condition": [
            {
                "condition": "template",
                "value_template": "{{ trigger.payload_json['after']['label'] in ['cat','dog'] }}",
            }
        ],
        "action": [
            {
                "service": notify_target,
                "data": {
                    "title": "{{ trigger.payload_json['after']['label']|capitalize }} detected",
                    "message": "in {{ trigger.payload_json['after']['camera'] }}",
                    "data": {
                        "image": "/api/frigate/notifications/{{ trigger.payload_json['after']['id'] }}/snapshot.jpg",
                    },
                },
            }
        ],
        "mode": "single",
    }


async def push_automation(base_url: str, token: str,
                           notify_target: str = "notify.mobile_app_phone"
                           ) -> tuple[bool, str]:
    """POST the Pawcorder automation to HA's config API. Idempotent —
    re-pushing replaces the existing automation with the same id."""
    payload = _build_automation_yaml(notify_target)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{base_url}/api/config/automation/config/{AUTOMATION_ID}"
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return False, str(exc)[:200]
    if resp.status_code in (200, 201):
        return True, "automation created"
    return False, f"HTTP {resp.status_code} {resp.text[:200]}"


async def list_notify_services(base_url: str, token: str) -> list[str]:
    """Return all `notify.*` services HA knows about. Lets the UI offer a
    dropdown of "which phone to notify" instead of asking the user to
    type ``notify.mobile_app_<your-phone-name>``."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{base_url}/api/services", headers=headers)
        except httpx.HTTPError:
            return []
    if resp.status_code != 200:
        return []
    services = resp.json() or []
    out: list[str] = []
    for domain in services:
        if domain.get("domain") != "notify":
            continue
        for name in (domain.get("services") or {}).keys():
            out.append(f"notify.{name}")
    return out
