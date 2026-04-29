"""Reolink E-series camera helpers: RTSP probe, login, enable RTSP service."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from . import camera_compat


@dataclass
class RtspProbeResult:
    ok: bool
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    error: str | None = None


def rtsp_url(ip: str, user: str, password: str, port: str | int = 554, sub: bool = False) -> str:
    """Build a Reolink E-series RTSP URL.

    Thin wrapper over ``camera_compat.build_rtsp_url("reolink", ...)`` so
    the URL-encode + path-template logic lives in one place. Kept as a
    free function (and re-exported) because both ``main.py`` and a few
    tests import it directly under this name.
    """
    return camera_compat.build_rtsp_url(
        "reolink", ip, user, password, port=int(port), sub=sub,
    )


async def probe_rtsp(url: str, timeout_seconds: int = 8) -> RtspProbeResult:
    """Use ffprobe to confirm we can read at least one video frame from the stream."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-print_format", "default=noprint_wrappers=1",
        "-show_entries", "stream=codec_name,width,height",
        "-i", url,
        "-timeout", str(timeout_seconds * 1_000_000),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds + 4)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return RtspProbeResult(ok=False, error="ffprobe timed out")
    except FileNotFoundError:
        return RtspProbeResult(ok=False, error="ffprobe not installed in admin container")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="ignore").strip().splitlines()[-1:]
        return RtspProbeResult(ok=False, error=err[0] if err else "ffprobe failed")

    info: dict[str, str] = {}
    for line in stdout.decode("utf-8", errors="ignore").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    return RtspProbeResult(
        ok=True,
        codec=info.get("codec_name"),
        width=int(info["width"]) if info.get("width", "").isdigit() else None,
        height=int(info["height"]) if info.get("height", "").isdigit() else None,
    )


# ---- Reolink HTTP API ----------------------------------------------------
# Reolink E-series cameras expose a CGI API at http://IP/cgi-bin/api.cgi.
# We use it to (a) verify credentials and (b) ensure RTSP is enabled.

class ReolinkClient:
    def __init__(self, ip: str, user: str, password: str, *, timeout: float = 6.0):
        self.ip = ip
        self.user = user
        self.password = password
        self.token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=f"http://{ip}",
            timeout=timeout,
            verify=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ReolinkClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def _post(self, cmd: str, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        url = "/cgi-bin/api.cgi"
        query = {"cmd": cmd}
        if self.token:
            query["token"] = self.token
        resp = await self._client.post(url, params=query, json=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected Reolink response shape: {data!r}")
        return data

    async def login(self) -> None:
        body = [{
            "cmd": "Login",
            "param": {
                "User": {"userName": self.user, "password": self.password, "Version": "0"},
            },
        }]
        data = await self._post("Login", body)
        first = data[0]
        if first.get("code") != 0:
            raise PermissionError(f"Reolink login failed: {first}")
        self.token = first["value"]["Token"]["name"]

    async def get_net_port(self) -> dict[str, Any]:
        body = [{"cmd": "GetNetPort", "action": 0, "param": {}}]
        data = await self._post("GetNetPort", body)
        if data[0].get("code") != 0:
            raise RuntimeError(f"GetNetPort failed: {data[0]}")
        return data[0]["value"]["NetPort"]

    async def enable_rtsp(self) -> None:
        """Ensure the RTSP service is enabled on port 554."""
        port = await self.get_net_port()
        if port.get("rtspEnable") == 1:
            return
        port["rtspEnable"] = 1
        if not port.get("rtspPort"):
            port["rtspPort"] = 554
        body = [{"cmd": "SetNetPort", "action": 0, "param": {"NetPort": port}}]
        data = await self._post("SetNetPort", body)
        if data[0].get("code") != 0:
            raise RuntimeError(f"SetNetPort failed: {data[0]}")

    async def device_info(self) -> dict[str, Any]:
        body = [{"cmd": "GetDevInfo", "action": 0, "param": {}}]
        data = await self._post("GetDevInfo", body)
        if data[0].get("code") != 0:
            raise RuntimeError(f"GetDevInfo failed: {data[0]}")
        return data[0]["value"]["DevInfo"]

    async def get_local_link(self) -> dict[str, Any] | None:
        """Returns the camera's network info (LAN vs WiFi). Returns None on failure
        rather than raising — connection type is informational, never load-bearing."""
        try:
            data = await self._post("GetLocalLink", [{"cmd": "GetLocalLink", "action": 0, "param": {}}])
        except Exception:  # noqa: BLE001
            return None
        first = data[0] if data else {}
        if first.get("code") != 0:
            return None
        return (first.get("value") or {}).get("LocalLink")


def classify_link(link: dict[str, Any] | None) -> str:
    """Map a Reolink GetLocalLink response to one of: 'wifi', 'wired', 'unknown'.

    The camera can't tell us whether wired power is from a PoE switch or a wall
    adapter, so we collapse both to 'wired'. PoE is just a power source, not a
    network protocol — Frigate doesn't care either way.
    """
    if not link:
        return "unknown"
    candidates = []
    for key in ("activeLink", "type", "connType"):
        v = link.get(key)
        if isinstance(v, str):
            candidates.append(v.lower())
    blob = " ".join(candidates)
    if "wifi" in blob or "wireless" in blob:
        return "wifi"
    if "lan" in blob or "ether" in blob or "wired" in blob:
        return "wired"
    return "unknown"


async def auto_configure(ip: str, user: str, password: str) -> dict[str, Any]:
    """Log in, enable RTSP if needed, return device info + connection type.
    Raises on auth failure; tolerates GetLocalLink failures (returns 'unknown').
    """
    async with ReolinkClient(ip, user, password) as c:
        await c.login()
        await c.enable_rtsp()
        info = await c.device_info()
        link = await c.get_local_link()
    return {
        "device": info,
        "link": link,
        "connection_type": classify_link(link),
        # Brand-dispatcher contract: every vendor module returns rtsp_main/sub
        # so callers don't need a separate URL-builder branch per brand.
        "rtsp_main": rtsp_url(ip, user, password, sub=False),
        "rtsp_sub":  rtsp_url(ip, user, password, sub=True),
    }
