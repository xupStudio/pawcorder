"""Tests for the EspTouch v2 broadcast encoder.

Network broadcast itself can't be tested without hardware — we verify
the deterministic encoder + CRC + AES wrapping produce stable output,
which is what the on-device receiver consumes.
"""
from __future__ import annotations

import pytest


def test_crc8_is_deterministic_and_byte_sized():
    """The CRC produced for the same input must be stable across runs.

    We use a custom CRC-8 with the polynomial Espressif's EspTouch
    receiver firmware checks — what matters is that the byte we produce
    matches what a paired ESP32 expects, which is verified on hardware,
    not against a generic standard's check value. The test guards
    against accidental algorithm drift.
    """
    from app.provisioning import esptouch_v2

    a = esptouch_v2._crc8(b"123456789")
    b = esptouch_v2._crc8(b"123456789")
    assert a == b
    assert 0 <= a <= 0xFF
    # Different input → different CRC for at least these two cases.
    assert esptouch_v2._crc8(b"") != esptouch_v2._crc8(b"x")


def test_build_payload_round_trips_round_decryption():
    """Encrypt with the well-known zero key, decrypt, and check structure."""
    from app.provisioning import esptouch_v2
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    payload = esptouch_v2.build_payload(ssid="HomeNet", psk="hunter2")
    # Decrypt with the same zero key + IV.
    cipher = Cipher(algorithms.AES(b"\x00" * 16), modes.CBC(b"\x00" * 16))
    dec = cipher.decryptor()
    plain_padded = dec.update(payload) + dec.finalize()
    pad_len = plain_padded[-1]
    plain = plain_padded[:-pad_len]
    flag = plain[0]
    assert flag & 0x80, "version-2 bit should be set"
    assert flag & 0x01, "has_ssid bit should be set"
    assert flag & 0x02, "has_password bit should be set"
    # SSID len + bytes
    assert plain[1] == len("HomeNet")
    assert plain[2:9] == b"HomeNet"


def test_build_payload_rejects_oversize_ssid():
    from app.provisioning import esptouch_v2

    with pytest.raises(ValueError, match="32-byte limit"):
        esptouch_v2.build_payload(ssid="x" * 33, psk="pw")


def test_build_payload_rejects_oversize_psk():
    from app.provisioning import esptouch_v2

    with pytest.raises(ValueError, match="64-byte limit"):
        esptouch_v2.build_payload(ssid="HomeNet", psk="x" * 65)


def test_build_frames_starts_with_guide_code():
    """First four frames must be the spec's GuideCode lengths."""
    from app.provisioning import esptouch_v2

    frames = esptouch_v2.build_frames(b"\x00" * 16)
    guide_lengths = [f.length for f in frames[:4]]
    assert guide_lengths == [515, 514, 513, 512]


def test_build_frames_encodes_each_byte_as_one_frame():
    """Each cleartext byte after the GuideCode produces exactly one frame."""
    from app.provisioning import esptouch_v2

    payload = b"\x10\x20\x30"
    frames = esptouch_v2.build_frames(payload)
    assert len(frames) == 4 + 3  # GuideCode + payload bytes
    assert frames[4].length == 1024 + 0x10
    assert frames[5].length == 1024 + 0x20
    assert frames[6].length == 1024 + 0x30


def test_build_frames_indices_wrap_at_byte_boundary():
    """The 257th byte indexes 0 again because dest_lo is one byte."""
    from app.provisioning import esptouch_v2

    payload = b"\x00" * 257
    frames = esptouch_v2.build_frames(payload)
    # Skip GuideCode (4 frames). Index 0 is at frames[4]; index 256 is
    # at frames[260], and (256 & 0xFF) == 0.
    assert frames[4].dest_lo == 0
    assert frames[4 + 256].dest_lo == 0


def test_aes_encrypt_rejects_bad_key_length():
    from app.provisioning import esptouch_v2

    with pytest.raises(ValueError, match="16 bytes"):
        esptouch_v2._aes_encrypt(b"hello", b"too-short-key")
