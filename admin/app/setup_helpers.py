"""Setup-wizard probes: brand-fingerprint a camera IP, flag WSL2 quirks."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import camera_compat, onvif_helper

logger = logging.getLogger("pawcorder.setup_helpers")

_ONVIF_PORTS = (80, 8000, 8080)

# Default credential pairs to try, ordered by frequency. Stops at the
# first hit. None of these grant elevated access on a sane camera —
# the user is supposed to change them at install time, but the vast
# majority don't, which is exactly why a probe is helpful here.
_DEFAULT_CREDS = (
    ("admin", "admin"),
    ("admin", ""),
    ("admin", "12345"),
    ("admin", "888888"),
    ("admin", "password"),
    ("root", "root"),
)

# Hard wall-clock cap so a non-camera device (open RTSP-but-not-ONVIF) can't
# hold up the wizard. With 5-way scan concurrency this caps the worst case
# at ~7s × ceil(N/5) instead of compounding per-credential timeouts.
_PROBE_BUDGET_SECONDS = 7.0

_NAME_SAFE_RE = re.compile(r"[^a-z0-9_]+")


@dataclass
class CameraProbe:
    ip: str
    onvif_port: int = 0
    rtsp_port: int = 554
    manufacturer: str = ""
    model: str = ""
    firmware_version: str = ""
    brand_guess: str = ""        # camera_compat key, e.g. "reolink"
    suggested_name: str = ""
    suggested_user: str = ""     # populated only if a default cred worked
    auth_succeeded: bool = False
    onvif_reachable: bool = False

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "onvif_port": self.onvif_port,
            "rtsp_port": self.rtsp_port,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "firmware_version": self.firmware_version,
            "brand_guess": self.brand_guess,
            "suggested_name": self.suggested_name,
            "suggested_user": self.suggested_user,
            "auth_succeeded": self.auth_succeeded,
            "onvif_reachable": self.onvif_reachable,
        }


async def _port_open(ip: str, port: int, timeout: float = 1.5) -> bool:
    """Return True if a TCP connection to ip:port completes."""
    try:
        fut = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def _find_onvif_port(ip: str) -> int:
    for port in _ONVIF_PORTS:
        if await _port_open(ip, port):
            return port
    return 0


def _guess_brand(manufacturer: str, model: str) -> str:
    """Map a free-form ONVIF manufacturer string to a camera_compat key."""
    blob = f"{manufacturer} {model}".lower()
    if not blob.strip():
        return ""
    # Order matters — Amcrest is a Dahua OEM, so we check it first.
    table = (
        ("reolink", ("reolink",)),
        ("hikvision", ("hikvision", "hilook")),
        ("amcrest", ("amcrest",)),
        ("dahua", ("dahua",)),
        ("axis", ("axis",)),
        ("foscam", ("foscam",)),
        ("tapo", ("tapo", "tp-link", "tp link")),
        ("imou", ("imou",)),
        ("wyze", ("wyze",)),
        ("unifi", ("unifi", "ubiquiti")),
    )
    for key, needles in table:
        if any(n in blob for n in needles):
            # Only return brands we actually have in the compat matrix —
            # otherwise the wizard would offer an unconfigurable brand.
            if key in camera_compat.BRANDS:
                return key
    return ""


def _suggest_name(manufacturer: str, model: str, ip: str) -> str:
    """Build a slug that satisfies cameras.yml's name regex.

    cameras_store enforces ``^[a-z][a-z0-9_]{0,30}$``. We prefer the
    manufacturer when present (most users want "reolink_42" rather than
    "rlc-410-42"), fall back to the IP's last octet, and always end up
    with a name that's safe to drop straight into the form.
    """
    last_octet = ip.rsplit(".", 1)[-1] if "." in ip else ""
    seed = (manufacturer or model or "camera").lower()
    slug = _NAME_SAFE_RE.sub("_", seed).strip("_")[:24] or "camera"
    if not slug[:1].isalpha():
        slug = "cam_" + slug
    if last_octet and last_octet.isdigit():
        slug = f"{slug}_{last_octet}"
    return slug[:31]


async def _try_default_creds(ip: str, port: int) -> tuple[dict, str] | None:
    """Try default credential pairs; return (info, username) at first hit."""
    for user, password in _DEFAULT_CREDS:
        try:
            info = await asyncio.wait_for(
                onvif_helper.get_device_information(ip, user, password, port=port),
                timeout=2.0,
            )
        except (asyncio.TimeoutError, PermissionError, httpx.HTTPError, OSError,
                RuntimeError, ValueError):
            continue
        return info, user
    return None


async def _anonymous_device_info(ip: str, port: int) -> dict | None:
    """Some firmwares answer GetDeviceInformation without auth. Try once."""
    try:
        info = await asyncio.wait_for(
            onvif_helper.get_device_information(ip, "", "", port=port),
            timeout=2.0,
        )
    except (asyncio.TimeoutError, PermissionError, httpx.HTTPError, OSError,
            RuntimeError, ValueError):
        return None
    return info if info.get("manufacturer") or info.get("model") else None


async def _probe_inner(ip: str) -> CameraProbe:
    probe = CameraProbe(ip=ip, suggested_name=_suggest_name("", "", ip))
    onvif_port = await _find_onvif_port(ip)
    if not onvif_port:
        return probe
    probe.onvif_port = onvif_port
    probe.onvif_reachable = True

    info = await _anonymous_device_info(ip, onvif_port)
    if info is None:
        result = await _try_default_creds(ip, onvif_port)
        if result is not None:
            info, probe.suggested_user = result
            probe.auth_succeeded = True

    if info:
        probe.manufacturer = info.get("manufacturer", "")
        probe.model = info.get("model", "")
        probe.firmware_version = info.get("firmware_version", "")
        probe.brand_guess = _guess_brand(probe.manufacturer, probe.model)
        probe.suggested_name = _suggest_name(probe.manufacturer, probe.model, ip)
    return probe


async def probe_camera(ip: str) -> CameraProbe:
    """Best-effort fingerprint at ``ip`` — never raises, always returns a row.

    Capped at _PROBE_BUDGET_SECONDS so a slow or unresponsive device
    can't stall the wizard's scan-then-probe pipeline.
    """
    try:
        return await asyncio.wait_for(_probe_inner(ip), timeout=_PROBE_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        return CameraProbe(ip=ip, suggested_name=_suggest_name("", "", ip))


async def probe_candidates(ips: list[str], *, concurrency: int = 5) -> list[CameraProbe]:
    """Probe several IPs in parallel. Bounded so we don't hammer a
    home router with 200 concurrent SOAP calls when scan_for_cameras
    returns an unusually large candidate list."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _wrapped(ip: str) -> CameraProbe:
        async with sem:
            return await probe_camera(ip)

    return await asyncio.gather(*(_wrapped(ip) for ip in ips))


# ---- environment quirks --------------------------------------------------

@dataclass
class EnvironmentQuirk:
    kind: str
    message_key: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message_key": self.message_key}


def _is_wsl() -> bool:
    try:
        text = Path("/proc/version").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "microsoft" in text.lower() or "wsl" in text.lower()


def detect_environment_quirks() -> list[EnvironmentQuirk]:
    """Surface non-fatal env notes — currently just WSL2 bridged-networking.

    Bridged mode silently breaks LAN scans (we see only the WSL virtual
    subnet). The mirrored-mode fix lives in .wslconfig, which the wizard
    points to via the i18n message_key.
    """
    if os.environ.get("PAWCORDER_FORCE_WSL_HINT") == "1" or _is_wsl():
        return [EnvironmentQuirk(kind="wsl2_bridged", message_key="SCAN_NO_HITS_WSL_HINT")]
    return []
