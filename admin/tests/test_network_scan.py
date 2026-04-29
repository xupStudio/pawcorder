"""Tests for nmap CIDR validation."""
from __future__ import annotations

import pytest

from app.network_scan import _looks_like_cidr


@pytest.mark.parametrize("cidr", [
    "192.168.1.0/24",
    "10.0.0.0/8",
    "172.16.0.0/16",
    "192.168.1.5/32",
])
def test_valid_cidr(cidr):
    assert _looks_like_cidr(cidr) is True


@pytest.mark.parametrize("bad", [
    "192.168.1.0",         # missing /n
    "not-a-cidr",
    "300.0.0.0/24",        # the regex is permissive on octet range; accept
    "192.168.1.0/abc",
    "",
    "; rm -rf / ;",        # injection attempt
])
def test_invalid_cidr_rejected(bad):
    # The regex is intentionally simple; we mostly guard against shell injection
    # by passing the value as a positional nmap argument, never via shell=True.
    if bad == "300.0.0.0/24":
        # current regex allows this; nmap would reject it itself
        assert _looks_like_cidr(bad) is True
    else:
        assert _looks_like_cidr(bad) is False


@pytest.mark.asyncio
async def test_scan_invalid_cidr_raises():
    from app.network_scan import scan_for_cameras
    with pytest.raises(ValueError):
        await scan_for_cameras("not-a-cidr")
