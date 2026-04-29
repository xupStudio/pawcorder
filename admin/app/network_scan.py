"""Network discovery for Reolink cameras.

We rely on nmap to find hosts with the RTSP port (554) open in a subnet
the user provides. We don't try to fingerprint Reolink specifically — any
RTSP-speaking device shows up as a candidate, and the user picks one.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass


@dataclass
class Candidate:
    ip: str
    rtsp_port: int = 554


_HOST_RE = re.compile(r"Nmap scan report for (?:[^ ]+ \()?([0-9.]+)\)?")
_PORT_OPEN_RE = re.compile(r"^(\d+)/tcp\s+open", re.MULTILINE)


async def scan_for_cameras(cidr: str, timeout_seconds: int = 60) -> list[Candidate]:
    """Run `nmap -p 554 --open` against the CIDR. Returns hosts with 554 open."""
    if not _looks_like_cidr(cidr):
        raise ValueError(f"Invalid CIDR: {cidr!r}")
    cmd = ["nmap", "-p", "554", "--open", "-T4", "-n", "-oG", "-", cidr]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("nmap scan timed out")

    if proc.returncode != 0:
        raise RuntimeError("nmap exited with a non-zero status")

    hits: list[Candidate] = []
    # Greppable output: "Host: 192.168.1.42 (...)\tPorts: 554/open/tcp//rtsp//..."
    for line in stdout.decode("utf-8", errors="ignore").splitlines():
        if not line.startswith("Host: "):
            continue
        if "554/open" not in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            hits.append(Candidate(ip=parts[1]))
    return hits


_CIDR_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$")


def _looks_like_cidr(value: str) -> bool:
    return bool(_CIDR_RE.match(value))
