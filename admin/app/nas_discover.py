"""mDNS / DNS-SD scanner for NAS devices on the local network.

Replaces the "type your NAS IP / share path from memory" friction on the
storage page. Browses for the well-known service types most consumer
NAS devices advertise:

  _smb._tcp.local         — Windows-style SMB shares (Synology, QNAP, …)
  _afpovertcp._tcp.local  — Apple Filing Protocol (older Synology, TrueNAS)
  _nfs._tcp.local         — NFS exports

Returns a deduplicated list of (name, ip, protocol) candidates. The
caller (UI) presents these as clickable rows that pre-fill the manual
form below.

Why we don't probe shares / list folders: those operations need
credentials, and we don't have them yet at discovery time. Discovery is
purely "what NAS-capable boxes do I see on this LAN" — credential prompt
comes after the user picks one.
"""
from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("pawcorder.nas_discover")

SERVICE_TYPES = (
    ("_smb._tcp.local.",        "smb"),
    ("_afpovertcp._tcp.local.", "afp"),
    ("_nfs._tcp.local.",        "nfs"),
)


@dataclass
class NasCandidate:
    """One discovered NAS device (one entry per protocol it advertises)."""
    name: str          # "Synology — DiskStation"
    host: str          # mDNS hostname like "diskstation.local"
    ip: str            # resolved A record (best-effort)
    protocol: str      # "smb" / "afp" / "nfs"
    port: int = 0      # advertised port (0 if not present)


def discover(timeout: float = 2.0) -> list[NasCandidate]:
    """Browse mDNS for NAS service types. Never raises.

    Single short-window scan — long enough to catch responses from
    devices on the same LAN, short enough to keep the UI snappy.
    """
    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceInfo
    except ImportError:
        logger.warning("zeroconf not installed — NAS discovery disabled")
        return []

    seen: dict[tuple[str, str], NasCandidate] = {}

    class _Listener:
        def __init__(self, zc: Zeroconf, protocol: str):
            self.zc = zc
            self.protocol = protocol

        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            try:
                info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            except Exception:  # noqa: BLE001 — zeroconf raises a grab-bag of types
                return
            if info is None:
                return
            display = name.removesuffix("." + type_).removesuffix(".") or name
            host = (info.server or "").rstrip(".") or display + ".local"
            ip = ""
            for addr in info.addresses_by_version(socket.AF_INET):
                # addresses_by_version yields packed bytes
                try:
                    ip = socket.inet_ntoa(addr)
                    break
                except OSError:
                    continue
            cand = NasCandidate(
                name=display, host=host, ip=ip,
                protocol=self.protocol, port=info.port or 0,
            )
            seen[(display, self.protocol)] = cand

        def remove_service(self, *_args, **_kwargs) -> None: pass
        def update_service(self, *_args, **_kwargs) -> None: pass

    zc: Optional["Zeroconf"] = None
    try:
        zc = Zeroconf()
        browsers = []
        for type_str, proto in SERVICE_TYPES:
            browsers.append(ServiceBrowser(zc, type_str, _Listener(zc, proto)))
        time.sleep(timeout)
    except OSError as exc:
        # No multicast group available (sandboxed CI, container without
        # network host mode, etc.). Silent — the UI still works manually.
        logger.warning("mDNS scan failed: %s", exc)
    finally:
        try:
            if zc is not None:
                zc.close()
        except Exception:  # noqa: BLE001
            pass

    return list(seen.values())
