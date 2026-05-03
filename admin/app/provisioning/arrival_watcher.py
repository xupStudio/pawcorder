"""Watch the LAN for cameras that just finished provisioning.

The provisioning pipeline ends with the camera asking the home AP for
DHCP. Pawcorder needs to know the moment that happens so it can:

  1. Resolve the camera's IP from its (already-known) MAC address.
  2. Hand the IP off to ``camera_setup.auto_configure_for_brand`` so
     the rest of the existing onboarding flow (RTSP enable, default
     credentials, ONVIF probe) runs end-to-end.
  3. Tell the user "all done — you can leave this page now".

We poll the kernel ARP table (``ip neigh`` on Linux, ``arp -an`` on
macOS) on a short interval. The watcher carries a list of *expected*
MACs (those for which a provisioner just ran ``provision`` and
returned ``needs_arrival_watcher=True``) and only fires for matches.
We don't watch DHCP server logs because most home routers don't expose
them readably.

Concurrent watches are fine — each ``ArrivalWatcher`` holds its own
in-memory MAC list and disposes when its task is cancelled.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from typing import AsyncIterator

from .base import normalise_mac

logger = logging.getLogger("pawcorder.provisioning.arrival_watcher")


@dataclass(frozen=True)
class Arrival:
    mac: str
    ip: str


_IP_NEIGH_RE = re.compile(
    r"^(?P<ip>[0-9.]+)\s+\S+\s+\S+\s+lladdr\s+(?P<mac>[0-9a-f:]{17})\s+",
    flags=re.IGNORECASE | re.MULTILINE,
)
_MACOS_ARP_RE = re.compile(
    r"\((?P<ip>[0-9.]+)\)\s+at\s+(?P<mac>[0-9a-f:]+)",
    flags=re.IGNORECASE,
)


import json as _json  # noqa: E402  — local alias to avoid touching downstream imports
import os as _os  # noqa: E402
import time as _time  # noqa: E402

# Path the host-side scanner writes the ARP snapshot to (see
# scripts/wifi-scan.sh). Inside the admin container this is bind-mounted
# from $PAWCORDER_DIR/.arp_scan.json. The container's own ARP table only
# sees other containers in the same Docker network; the host snapshot is
# what lets us detect a freshly-paired camera joining the LAN, especially
# on macOS where Docker Desktop hides the LAN from the container entirely.
_HOST_ARP_FILE = _os.environ.get("ARP_SCAN_FILE", "/data/.arp_scan.json")
# Snapshot considered usable for this many seconds after generated_at.
# Paired with the scanner's StartInterval (30s default) — 90s covers two
# missed runs without surprising the user with stale results.
_HOST_ARP_MAX_AGE = 90


def _host_arp_snapshot() -> dict[str, str] | None:
    """Read MAC→IP from the host's ARP snapshot file, or ``None`` if it
    doesn't exist / is stale / is corrupt. Falls through to the
    in-container ``arp`` / ``ip neigh`` path so Linux installs without
    the host helper still work."""
    try:
        st = _os.stat(_HOST_ARP_FILE)
    except (FileNotFoundError, PermissionError):
        return None
    if (_time.time() - st.st_mtime) > _HOST_ARP_MAX_AGE:
        return None
    try:
        with open(_HOST_ARP_FILE) as f:
            data = _json.load(f)
    except (OSError, _json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    out: dict[str, str] = {}
    for n in data.get("neighbors") or []:
        if not isinstance(n, dict):
            continue
        mac = normalise_mac(n.get("mac") or "")
        ip = (n.get("ip") or "").strip()
        if mac and ip and mac != "00:00:00:00:00:00":
            out[mac] = ip
    return out


async def _arp_snapshot() -> dict[str, str]:
    """Return ``{normalised_mac: ip}`` from the host's ARP cache.

    Prefers the host-helper snapshot (``.arp_scan.json``) so we see the
    real LAN even when running inside a Docker container that's
    isolated from the host network namespace.
    """
    host = _host_arp_snapshot()
    if host is not None:
        return host
    if sys.platform == "darwin":
        proc = await asyncio.create_subprocess_exec(
            "arp", "-an",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", errors="ignore")
        result: dict[str, str] = {}
        for m in _MACOS_ARP_RE.finditer(text):
            mac = normalise_mac(m.group("mac"))
            if mac and mac != "00:00:00:00:00:00":
                result[mac] = m.group("ip")
        return result

    if shutil.which("ip"):
        proc = await asyncio.create_subprocess_exec(
            "ip", "neigh",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        result = {}
        for m in _IP_NEIGH_RE.finditer(out.decode("utf-8", errors="ignore")):
            mac = normalise_mac(m.group("mac"))
            if mac and mac != "00:00:00:00:00:00":
                result[mac] = m.group("ip")
        return result

    # Fallback: read /proc/net/arp directly. This works in any minimal
    # Linux container that doesn't bundle ``ip``.
    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    result = {}
    for line in lines[1:]:  # header
        parts = line.split()
        if len(parts) < 4:
            continue
        ip, _, _, mac = parts[0], parts[1], parts[2], parts[3]
        mac = normalise_mac(mac)
        if mac and mac != "00:00:00:00:00:00":
            result[mac] = ip
    return result


class ArrivalWatcher:
    """Polls ARP and emits ``Arrival`` events for matching MACs.

    Usage::

        watcher = ArrivalWatcher()
        watcher.expect("aa:bb:cc:dd:ee:ff")
        async for arrival in watcher.stream(timeout_s=120):
            print(arrival.ip)

    Adding a MAC after ``stream()`` has been started is allowed — the
    watcher picks up new expectations on its next poll cycle.
    """

    def __init__(self, *, poll_interval_s: float = 2.0) -> None:
        self._expected: set[str] = set()
        self._seen: set[str] = set()
        self._poll_interval_s = poll_interval_s

    def expect(self, mac: str) -> None:
        m = normalise_mac(mac)
        if m and m != "00:00:00:00:00:00":
            self._expected.add(m)

    def cancel(self, mac: str) -> None:
        self._expected.discard(normalise_mac(mac))

    @property
    def expected_macs(self) -> list[str]:
        return sorted(self._expected)

    async def stream(self, *, timeout_s: float = 120.0) -> AsyncIterator[Arrival]:
        """Yield ``Arrival`` for each expected MAC the kernel sees.

        Each MAC fires at most once per stream — once we hand off to
        ``camera_setup``, re-firing would loop the orchestrator.
        Cancellation through ``timeout_s`` is hard — caller can wrap
        the stream in ``asyncio.wait_for`` if a softer bound is wanted.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline and self._expected:
            snapshot = await _arp_snapshot()
            for mac in list(self._expected):
                if mac in self._seen:
                    continue
                ip = snapshot.get(mac)
                if ip:
                    self._seen.add(mac)
                    yield Arrival(mac=mac, ip=ip)
            if not self._expected - self._seen:
                # Everything we were waiting for has arrived — exit
                # rather than burn the poll budget.
                return
            await asyncio.sleep(self._poll_interval_s)
