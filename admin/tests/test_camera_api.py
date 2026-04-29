"""Tests for the Reolink classify_link helper + RTSP URL builder."""
from __future__ import annotations

import pytest

from app.camera_api import classify_link, rtsp_url


@pytest.mark.parametrize("link, expected", [
    ({"activeLink": "WiFi"},      "wifi"),
    ({"activeLink": "wifi"},      "wifi"),
    ({"activeLink": "LAN"},       "wired"),
    ({"activeLink": "lan"},       "wired"),
    ({"type": "Wireless"},        "wifi"),
    ({"type": "Ethernet"},        "wired"),
    ({"connType": "wired"},       "wired"),
    ({"activeLink": "cellular"},  "unknown"),
    ({},                          "unknown"),
    (None,                        "unknown"),
])
def test_classify_link(link, expected):
    assert classify_link(link) == expected


def test_rtsp_url_builds_main_and_sub():
    assert rtsp_url("1.2.3.4", "admin", "pw", port=554, sub=False) == \
        "rtsp://admin:pw@1.2.3.4:554/h264Preview_01_main"
    assert rtsp_url("1.2.3.4", "admin", "pw", port=554, sub=True) == \
        "rtsp://admin:pw@1.2.3.4:554/h264Preview_01_sub"


def test_rtsp_url_custom_port():
    assert "10550" in rtsp_url("1.2.3.4", "admin", "pw", port=10550)
