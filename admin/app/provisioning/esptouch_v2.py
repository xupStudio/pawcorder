"""Espressif EspTouch v2 broadcast provisioner.

EspTouch is Espressif's "shout the credentials over Wi-Fi multicast and
let the device sniff them" protocol. It works because an ESP32 can
listen in promiscuous mode for 802.11 frames whose headers / lengths
encode bytes, even when the device isn't associated with any AP.

v1 encoded data in UDP packet *lengths*. v2 (the only flavour Espressif
still recommends) encodes data in destination-MAC + length and adds
AES-128/CBC encryption with a shared 16-byte key the camera firmware
ships pre-set. EspTouch v2 also supports a "no encryption" mode (key
all-zeros) which is what cheap whitebrand cams ship; we default to
that with a clear flag for the user to override.

Reference: ``EsptouchForAndroid/esptouch-v2`` (Apache-2.0). We port the
encoder faithfully and abstract the network broadcast behind an async
helper so unit tests can assert the byte sequence without sending real
UDP traffic.

Caveats kept honest:
  * EspTouch v2 needs the host on the SAME 2.4 GHz Wi-Fi network the
    target camera will eventually join. 5 GHz hosts cannot drive this
    flow — the orchestrator surfaces a hint when the host is on 5 GHz.
  * Multicast / broadcast must traverse the AP. Many enterprise APs
    isolate clients and EspTouch silently fails. Home routers are fine.
  * Cameras that ship with a custom AES key have to be paired with the
    matching key, normally taken from a sticker. ``aes_key`` parameter
    accepts the 16-byte key when the user provides it.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .base import (
    BaseProvisioner,
    DiscoveredDevice,
    ProvisionerResult,
    ProvisioningRequest,
)

logger = logging.getLogger("pawcorder.provisioning.esptouch")


# Per Espressif's spec, the EspTouch broadcast loop runs on a fixed
# interval. Every byte is sent multiple times so the camera reliably
# captures frames despite Wi-Fi loss.
_FRAME_INTERVAL_MS = 8
_RUN_DURATION_S = 60.0
_MULTICAST_BASE = "234.0.0.0"
_MULTICAST_PORT = 7001

# Sync header pattern — the camera looks for this to know "data starts now".
# The four magic lengths are the v2 spec's GuideCode.
_GUIDE_CODE_LENGTHS = (515, 514, 513, 512)


@dataclass
class _Frame:
    """One data frame queued for broadcast.

    ``length`` is the UDP payload length the frame must occupy.
    ``dest_lo`` is the low byte of the multicast destination IP. The
    receiver reconstructs the byte stream from these two fields.
    """
    length: int
    dest_lo: int


def _aes_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128/CBC encrypt with PKCS7 padding (default v2 pad)."""
    if len(key) != 16:
        raise ValueError("aes_key must be 16 bytes")
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len
    iv = b"\x00" * 16  # v2 uses zero IV — relies on per-payload structure for uniqueness
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _crc8(data: bytes) -> int:
    """CRC-8-MAXIM polynomial 0x31 used by EspTouch v2."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc & 0xFF


def build_payload(
    *,
    ssid: str,
    psk: str,
    aes_key: bytes = b"\x00" * 16,
    reserved: bytes = b"",
) -> bytes:
    """Construct the encrypted EspTouch v2 payload.

    Layout (per esptouch-v2 source):
      flag (1B): bit0=has_ssid, bit1=has_pwd, bit2=has_reserved, bit7=v2
      ssid_len (1B) + ssid bytes
      pwd_len (1B) + pwd bytes
      reserved_len (1B) + reserved bytes
      crc (1B)
      → AES-128/CBC encrypt the whole thing with the shared key
    """
    ssid_b = ssid.encode("utf-8")
    pwd_b = psk.encode("utf-8")
    if len(ssid_b) > 32:
        raise ValueError("SSID exceeds 32-byte limit")
    if len(pwd_b) > 64:
        raise ValueError("password exceeds EspTouch's 64-byte limit")
    flag = 0x80 | 0x01  # v2 + has_ssid
    if pwd_b:
        flag |= 0x02
    if reserved:
        flag |= 0x04
    body = bytes([flag])
    body += bytes([len(ssid_b)]) + ssid_b
    body += bytes([len(pwd_b)]) + pwd_b
    body += bytes([len(reserved)]) + reserved
    body += bytes([_crc8(body)])
    return _aes_encrypt(body, aes_key)


def build_frames(payload: bytes) -> list[_Frame]:
    """Split the encrypted payload into the broadcast frame sequence.

    Each cleartext byte ``b`` gets mapped to a frame whose UDP payload
    length is ``1024 + b`` and whose destination IP is
    ``234.<index>.<index_high>.<index_low>``. The receiver uses the
    length-1024 to recover the byte and the destination's low octet to
    recover the index, which lets it reorder packets even if Wi-Fi
    drops frames.
    """
    frames: list[_Frame] = []
    # Lead with the GuideCode so the receiver knows where the data starts.
    for length in _GUIDE_CODE_LENGTHS:
        frames.append(_Frame(length=length, dest_lo=0))
    # Then every byte of payload as one frame. Each byte is padded to
    # 1024+b so the receiver decodes 0..255 from the frame length.
    for index, byte in enumerate(payload):
        frames.append(_Frame(length=1024 + byte, dest_lo=index & 0xFF))
    return frames


async def _broadcast_loop(
    frames: list[_Frame],
    *,
    duration_s: float = _RUN_DURATION_S,
    interval_ms: int = _FRAME_INTERVAL_MS,
) -> None:
    """Send the frame sequence repeatedly for ``duration_s``.

    We send the same sequence over and over until either the user
    cancels (the orchestrator's task is cancelled) or the budget
    elapses. The camera's promiscuous-mode scanner takes several full
    cycles to re-assemble the data on a busy 2.4 GHz channel.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration_s
        interval_s = interval_ms / 1000.0
        while loop.time() < deadline:
            for frame in frames:
                # Pack to ``frame.length`` bytes — the *length* is what
                # carries data, the contents themselves are zeros.
                payload = b"\x00" * frame.length
                ip = f"234.{frame.dest_lo}.0.0"
                try:
                    sock.sendto(payload, (ip, _MULTICAST_PORT))
                except OSError as exc:
                    logger.debug("esptouch broadcast send failed: %s", exc)
                await asyncio.sleep(interval_s)
    finally:
        sock.close()


async def push_creds(
    *,
    home_ssid: str,
    home_psk: str,
    aes_key: bytes = b"\x00" * 16,
    duration_s: float = _RUN_DURATION_S,
) -> ProvisionerResult:
    payload = build_payload(ssid=home_ssid, psk=home_psk, aes_key=aes_key)
    frames = build_frames(payload)
    try:
        await _broadcast_loop(frames, duration_s=duration_s)
    except asyncio.CancelledError:
        # User cancelled — surface a benign result rather than a stack trace.
        return ProvisionerResult(
            ok=False, transport="esptouch", needs_arrival_watcher=False,
            message="EspTouch broadcast cancelled",
        )
    return ProvisionerResult(
        ok=True, transport="esptouch", needs_arrival_watcher=True,
        message=(
            "Broadcast Wi-Fi settings via EspTouch for "
            f"{int(duration_s)} seconds. Watching for the camera to join…"
        ),
    )


class EspTouchProvisioner(BaseProvisioner):
    transport = "esptouch"
    capability = "broadcast"

    @classmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        return device.transport == "esptouch"

    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        return await push_creds(
            home_ssid=request.ssid,
            home_psk=request.psk,
            aes_key=request.device.extra.get("aes_key", b"\x00" * 16),
        )
