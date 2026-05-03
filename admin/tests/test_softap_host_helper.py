"""Host-helper backend tests for softap_scanner.

The admin runs in a Docker container that can't see the host's Wi-Fi
interface, so install.sh installs a host-side scanner (launchd / systemd
timer) that drops a JSON snapshot at /data/.wifi_scan.json. These tests
exercise the file-based backend in isolation by pointing the env var
WIFI_SCAN_FILE at a tmp file and stuffing fixtures through it.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def helper_file(tmp_path, monkeypatch):
    """Redirect softap_scanner at a tmp .wifi_scan.json and reload the
    module so its module-level _HOST_HELPER_FILE constant picks it up."""
    path = tmp_path / "wifi_scan.json"
    monkeypatch.setenv("WIFI_SCAN_FILE", str(path))
    # softap_scanner reads _HOST_HELPER_FILE at import; re-import after
    # the env override so the test sees our path, not whatever was loaded
    # from a prior test.
    import app.provisioning.softap_scanner as m
    importlib.reload(m)
    return path


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_no_helper_file_means_unavailable(helper_file):
    from app.provisioning import softap_scanner
    assert softap_scanner.softap_scanner_available() is False
    # Reason "stale" because the file doesn't exist yet — distinguishable
    # from no_wifi_iface (genuinely no Wi-Fi card on the host).
    assert softap_scanner.softap_scanner_unavailable_reason() == "stale"


def test_fresh_helper_file_with_networks_makes_scanner_available(helper_file):
    _write(helper_file, {
        "schema": 1, "generated_at": int(time.time()),
        "platform": "macos", "tool": "system_profiler",
        "networks": [{"ssid": "MyWifi", "bssid": "", "signal_dbm": -45, "channel": 6}],
        "error": None,
    })
    from app.provisioning import softap_scanner
    assert softap_scanner.softap_scanner_available() is True
    assert softap_scanner.softap_scanner_unavailable_reason() == ""


def test_fresh_helper_with_no_networks_still_makes_scanner_available(helper_file):
    # An empty list means "scanned successfully, nothing nearby" — the
    # banner should NOT appear in this case (the user's UX hint should
    # be "no SoftAP cameras found", not "scanning broken").
    _write(helper_file, {
        "schema": 1, "generated_at": int(time.time()),
        "platform": "macos", "tool": "system_profiler",
        "networks": [], "error": "no_networks_seen",
    })
    from app.provisioning import softap_scanner
    assert softap_scanner.softap_scanner_available() is True


def test_no_wifi_iface_propagates_as_reason(helper_file):
    _write(helper_file, {
        "schema": 1, "generated_at": int(time.time()),
        "platform": "linux", "tool": "iw",
        "networks": [], "error": "no_wifi_iface",
    })
    from app.provisioning import softap_scanner
    assert softap_scanner.softap_scanner_available() is False
    assert softap_scanner.softap_scanner_unavailable_reason() == "no_wifi_iface"


def test_stale_helper_file_falls_back_to_in_container_tools(helper_file, monkeypatch):
    # File written 200s ago — older than _HOST_HELPER_MAX_AGE (90s).
    _write(helper_file, {
        "schema": 1, "generated_at": int(time.time()) - 200,
        "platform": "macos", "tool": "system_profiler",
        "networks": [{"ssid": "stale", "bssid": "", "signal_dbm": -50, "channel": 1}],
        "error": None,
    })
    import os
    os.utime(helper_file, (time.time() - 200, time.time() - 200))
    from app.provisioning import softap_scanner
    # No nmcli/iw/airport/netsh in test env, so once we ignore the stale
    # file we should report unavailable.
    monkeypatch.setattr(softap_scanner.shutil, "which", lambda _: None)
    assert softap_scanner.softap_scanner_available() is False
    assert softap_scanner.softap_scanner_unavailable_reason() == "stale"


def test_scan_once_returns_fingerprinted_devices_from_helper(helper_file):
    # SSIDs that match known SoftAP fingerprints must be emitted as
    # DiscoveredDevice. Unknown SSIDs are dropped (caller only cares
    # about cameras).
    _write(helper_file, {
        "schema": 1, "generated_at": int(time.time()),
        "platform": "macos", "tool": "system_profiler",
        "networks": [
            {"ssid": "FOSCAM_AABBCC", "bssid": "", "signal_dbm": -40, "channel": 1},
            {"ssid": "MyHomeWifi",   "bssid": "", "signal_dbm": -55, "channel": 6},
        ],
        "error": None,
    })
    from app.provisioning import softap_scanner
    devices = asyncio.run(softap_scanner.scan_once())
    # Expectation: at least the Foscam SoftAP makes it through; the
    # plain "MyHomeWifi" doesn't match any fingerprint and is dropped.
    matched = [d for d in devices if "FOSCAM" in d.ssid.upper()]
    assert len(matched) == 1
    assert matched[0].transport == "softap"


def test_corrupt_helper_file_is_ignored(helper_file):
    helper_file.write_text("not valid json {{{")
    from app.provisioning import softap_scanner
    assert softap_scanner.softap_scanner_available() is False
    # Treated as missing/stale — no crash.
    devices = asyncio.run(softap_scanner.scan_once())
    assert devices == []


def test_wrong_schema_is_ignored(helper_file):
    _write(helper_file, {
        "schema": 99, "generated_at": int(time.time()),
        "platform": "macos", "tool": "system_profiler",
        "networks": [{"ssid": "anything"}], "error": None,
    })
    from app.provisioning import softap_scanner
    assert softap_scanner.softap_scanner_available() is False
