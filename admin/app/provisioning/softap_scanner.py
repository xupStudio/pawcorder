"""SoftAP discovery — find cameras broadcasting their own Wi-Fi.

Cameras in setup mode that don't speak BLE often open a SoftAP — they
become a Wi-Fi access point with an SSID like ``Foscam_xxxx`` or
``IPC-AABBCCDD``. Pawcorder asks the OS to enumerate visible SSIDs,
runs each through ``fingerprints.match_softap``, and yields a
``DiscoveredDevice`` per match.

Backends used (auto-detected):

  * ``nmcli`` — modern Linux / WSL2 with NetworkManager
  * ``iwlist`` / ``iw dev <iface> scan`` — fallback for non-NM hosts
  * ``airport`` — macOS (built-in ``airport -s``)
  * ``netsh wlan show networks mode=Bssid`` — Windows / WSL2-via-PowerShell

We never *join* the SoftAP from this module — that's the per-vendor
softap_*.py provisioner's job. Joining changes the host's network
state, and we want to make that explicit (and reversible) at the
moment the user actually picks a camera to set up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass

from .base import DiscoveredDevice, normalise_mac
from .fingerprints import match_softap

logger = logging.getLogger("pawcorder.provisioning.softap")


@dataclass
class _APSighting:
    ssid: str
    bssid: str = ""
    signal_dbm: int = 0


# ---------------------------------------------------------------------------
# Host-helper snapshot (preferred backend)
# ---------------------------------------------------------------------------
#
# Why a file rather than an exec: this module runs in the admin Docker
# container. On macOS Docker Desktop runs Linux in a VM that has no
# Wi-Fi card; even on Linux a containerised nmcli/iw can't reach the
# host's NetworkManager socket. So install.sh installs a small host-side
# scanner (launchd on Mac, systemd timer on Linux) that writes the
# snapshot below, and we read it from the bind-mounted /data volume.
#
# Schema is `scripts/wifi-scan.sh`'s output (schema=1):
#   {
#     "schema": 1, "generated_at": <unix-seconds>,
#     "platform": "macos"|"linux", "tool": "system_profiler"|"nmcli"|"iw"|"none",
#     "networks": [ {"ssid","bssid","signal_dbm","channel"}, ... ],
#     "error": null | "no_wifi_iface" | "no_networks_seen" | ...
#   }

_HOST_HELPER_FILE = os.environ.get("WIFI_SCAN_FILE", "/data/.wifi_scan.json")
# Snapshot considered usable for this many seconds after generated_at.
# Set to 3× the scanner's StartInterval so a single skipped run doesn't
# blank out the UI; longer than that and the user is staring at stale
# SSIDs while reset-ing their camera.
_HOST_HELPER_MAX_AGE = 90
# Reasons that mean "host genuinely cannot scan" (vs. "scan ran but saw
# nothing right now"). The first set turns the page banner on; the
# second leaves it off and just shows an empty list.
_HOST_HELPER_FATAL = {"no_wifi_iface", "no_python", "no_scan_tool",
                      "system_profiler_failed", "iw_scan_failed",
                      "unsupported_platform"}


def _host_helper_payload() -> dict | None:
    """Return parsed snapshot iff present and not stale, else ``None``."""
    try:
        st = os.stat(_HOST_HELPER_FILE)
    except (FileNotFoundError, PermissionError):
        return None
    if (time.time() - st.st_mtime) > _HOST_HELPER_MAX_AGE:
        return None
    try:
        with open(_HOST_HELPER_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    return data


def _host_helper_status() -> tuple[bool, str]:
    """``(available, reason)`` — reason is one of:

    * ``""`` when the helper is producing fresh, scannable snapshots.
    * ``"stale"`` if the snapshot is missing or older than the max age.
    * one of ``_HOST_HELPER_FATAL`` reasons when scanning is impossible.
    """
    p = _host_helper_payload()
    if p is None:
        return False, "stale"
    err = p.get("error")
    if err in _HOST_HELPER_FATAL:
        return False, str(err)
    return True, ""


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def _which_scanner() -> str:
    """Return ``"nmcli"`` / ``"iw"`` / ``"airport"`` / ``"netsh"`` / ``""``."""
    for tool, key in (
        ("nmcli", "nmcli"),
        ("airport", "airport"),
        ("iw", "iw"),
        ("netsh", "netsh"),
    ):
        if shutil.which(tool):
            return key
    return ""


def softap_scanner_available() -> bool:
    """True when *some* scan path is workable.

    Host-helper snapshot wins when present and fresh; otherwise we fall
    back to whatever in-container CLI is available (nmcli on Linux+host
    network mode, ``iw`` on bare-metal hosts that copied the binary in,
    etc.). Returning False here makes the wireless onboarding page show
    the "this host can't scan Wi-Fi" banner.
    """
    available, _ = _host_helper_status()
    if available:
        return True
    return bool(_which_scanner())


def softap_scanner_unavailable_reason() -> str:
    """Empty string when scanning works; otherwise a short, stable code
    the UI can use to swap the banner copy. Distinguishes "you're on a
    wired-only server" from "the scanner just hasn't run yet"."""
    available, reason = _host_helper_status()
    if available:
        return ""
    if _which_scanner():
        return ""
    return reason or "no_scan_tool"


# ---------------------------------------------------------------------------
# Per-tool parsers
# ---------------------------------------------------------------------------


_NMCLI_FIELDS = "SSID,BSSID,SIGNAL"


async def _scan_nmcli() -> list[_APSighting]:
    proc = await asyncio.create_subprocess_exec(
        "nmcli", "-t", "-f", _NMCLI_FIELDS, "device", "wifi", "list", "--rescan", "yes",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    sightings: list[_APSighting] = []
    for raw in out.decode("utf-8", errors="ignore").splitlines():
        # nmcli's terse format escapes ``:`` inside fields with a
        # backslash. We read the BSSID first (always 17 chars),
        # signal last (always digits), and treat everything in between
        # as the SSID — which preserves SSIDs containing ``:``.
        line = raw.replace(r"\:", "\x00")
        parts = line.split(":")
        if len(parts) < 8:
            continue
        signal = parts[-1]
        bssid = ":".join(parts[-7:-1])
        ssid = ":".join(parts[:-7]).replace("\x00", ":")
        try:
            signal_int = int(signal) if signal else 0
        except ValueError:
            signal_int = 0
        sightings.append(
            _APSighting(
                ssid=ssid,
                bssid=normalise_mac(bssid),
                # nmcli reports signal as a 0–100 percentage. Convert to
                # an approximate dBm using the standard linear mapping:
                # 100% ≈ -30 dBm, 0% ≈ -100 dBm.
                signal_dbm=-30 - int((100 - signal_int) * 0.7),
            )
        )
    return sightings


_AIRPORT_LINE_RE = re.compile(
    r"^\s*(.{1,32}?)\s+([0-9a-f:]{17})\s+(-?\d+)",
    flags=re.IGNORECASE,
)


async def _scan_airport() -> list[_APSighting]:
    # macOS airport binary is at a fixed path (the symlink in /usr/local
    # is no longer made on Apple Silicon by default).
    binary = shutil.which("airport") or "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
    proc = await asyncio.create_subprocess_exec(
        binary, "-s",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    sightings: list[_APSighting] = []
    text = out.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    if not lines:
        return sightings
    for line in lines[1:]:  # skip header
        m = _AIRPORT_LINE_RE.match(line)
        if not m:
            continue
        ssid, bssid, rssi = m.groups()
        sightings.append(
            _APSighting(
                ssid=ssid.strip(),
                bssid=normalise_mac(bssid),
                signal_dbm=int(rssi),
            )
        )
    return sightings


_IW_SSID_RE = re.compile(r"^\s+SSID:\s*(.*)$")
_IW_BSS_RE = re.compile(r"^BSS\s+([0-9a-f:]{17})", re.IGNORECASE)
_IW_SIGNAL_RE = re.compile(r"^\s+signal:\s*(-?\d+\.?\d*)\s*dBm")


async def _scan_iw(interface: str) -> list[_APSighting]:
    proc = await asyncio.create_subprocess_exec(
        "iw", "dev", interface, "scan",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    sightings: list[_APSighting] = []
    cur: _APSighting | None = None
    for line in out.decode("utf-8", errors="ignore").splitlines():
        m = _IW_BSS_RE.match(line)
        if m:
            if cur and cur.ssid:
                sightings.append(cur)
            cur = _APSighting(ssid="", bssid=normalise_mac(m.group(1)))
            continue
        if cur is None:
            continue
        m = _IW_SSID_RE.match(line)
        if m:
            cur.ssid = m.group(1)
            continue
        m = _IW_SIGNAL_RE.match(line)
        if m:
            try:
                cur.signal_dbm = int(float(m.group(1)))
            except ValueError:
                cur.signal_dbm = 0
    if cur and cur.ssid:
        sightings.append(cur)
    return sightings


async def _wireless_iface_iw() -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "iw", "dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    for line in out.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            return line.split(" ", 1)[1].strip()
    return None


_NETSH_BSSID_RE = re.compile(r"^\s+BSSID\s+\d+\s+:\s*([0-9a-f:]{17})", re.IGNORECASE)
_NETSH_SSID_RE = re.compile(r"^SSID\s+\d+\s+:\s*(.*)$")
_NETSH_SIGNAL_RE = re.compile(r"^\s+Signal\s+:\s*(\d+)%")


async def _scan_netsh() -> list[_APSighting]:
    proc = await asyncio.create_subprocess_exec(
        "netsh", "wlan", "show", "networks", "mode=Bssid",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", errors="ignore")
    sightings: list[_APSighting] = []
    cur_ssid = ""
    cur_bssid = ""
    cur_signal = 0
    for line in text.splitlines():
        m = _NETSH_SSID_RE.match(line)
        if m:
            if cur_bssid:
                sightings.append(_APSighting(
                    ssid=cur_ssid,
                    bssid=normalise_mac(cur_bssid),
                    signal_dbm=-30 - int((100 - cur_signal) * 0.7),
                ))
                cur_bssid = ""
            cur_ssid = m.group(1).strip()
            continue
        m = _NETSH_BSSID_RE.match(line)
        if m:
            if cur_bssid:
                sightings.append(_APSighting(
                    ssid=cur_ssid,
                    bssid=normalise_mac(cur_bssid),
                    signal_dbm=-30 - int((100 - cur_signal) * 0.7),
                ))
            cur_bssid = m.group(1)
            continue
        m = _NETSH_SIGNAL_RE.match(line)
        if m:
            try:
                cur_signal = int(m.group(1))
            except ValueError:
                cur_signal = 0
    if cur_bssid:
        sightings.append(_APSighting(
            ssid=cur_ssid,
            bssid=normalise_mac(cur_bssid),
            signal_dbm=-30 - int((100 - cur_signal) * 0.7),
        ))
    return sightings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _scan_host_helper() -> list[_APSighting]:
    """Read sightings from the host-side scanner's JSON snapshot."""
    p = _host_helper_payload()
    if p is None:
        return []
    out: list[_APSighting] = []
    for n in p.get("networks", []):
        if not isinstance(n, dict):
            continue
        ssid = (n.get("ssid") or "").strip()
        if not ssid:
            continue
        try:
            sig = int(n.get("signal_dbm") or 0)
        except (TypeError, ValueError):
            sig = 0
        out.append(_APSighting(
            ssid=ssid,
            bssid=normalise_mac(n.get("bssid") or ""),
            signal_dbm=sig,
        ))
    return out


async def scan_once() -> list[DiscoveredDevice]:
    """Run the platform's Wi-Fi scanner and emit fingerprinted devices.

    Returns a list deduped by SSID — running the same scan twice in
    quick succession yields the same SSID for a stable AP, and the UI
    re-rendering on every duplicate would flicker badly.
    """
    sightings: list[_APSighting] = []
    helper_ok, _reason = _host_helper_status()
    tool = "" if helper_ok else _which_scanner()
    try:
        if helper_ok:
            sightings = _scan_host_helper()
        elif tool == "nmcli":
            sightings = await _scan_nmcli()
        elif tool == "airport":
            sightings = await _scan_airport()
        elif tool == "iw":
            iface = await _wireless_iface_iw()
            if iface:
                sightings = await _scan_iw(iface)
        elif tool == "netsh":
            sightings = await _scan_netsh()
        else:
            logger.info("no Wi-Fi scan tool found; SoftAP discovery disabled")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wi-Fi scan via %s failed: %s", tool or "host_helper", exc)
        return []

    devices: dict[str, DiscoveredDevice] = {}
    for s in sightings:
        if not s.ssid:
            continue
        fp = match_softap(s.ssid)
        if fp is None:
            continue
        device = DiscoveredDevice(
            id=s.bssid or s.ssid,
            transport="softap",
            vendor=fp.vendor,
            model=fp.label,
            label=s.ssid,
            mac=s.bssid,
            ssid=s.ssid,
            signal_dbm=s.signal_dbm,
            capability=fp.capability,
            fingerprint_id=fp.id,
            extra={
                "softap_ip": fp.metadata.get("softap_ip", ""),
                # Vendor-app deep links for capability=="vendor" devices
                # (Tapo / SmartLife / Wyze etc.). Forwarding them here
                # lets the UI render the App Store / Play buttons in the
                # handoff card without a second round-trip to the server.
                "vendor_app_ios": fp.metadata.get("vendor_app_ios", ""),
                "vendor_app_android": fp.metadata.get("vendor_app_android", ""),
            },
        )
        # Newer-RSSI-wins so the UI shows the closest copy of a SoftAP
        # if the user has more than one camera in setup mode.
        existing = devices.get(device.id)
        if existing is None or device.signal_dbm > existing.signal_dbm:
            devices[device.id] = device
    return list(devices.values())
