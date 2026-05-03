"""Host-helper backend tests for arrival_watcher.

Same architectural reason as test_softap_host_helper: the admin runs
inside Docker and can't see the host's ARP table, so install.sh installs
a launchd / systemd helper that writes a snapshot to /data/.arp_scan.json
which the watcher reads. These tests stub the file path via
``ARP_SCAN_FILE`` and reload the module so the constants pick up.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def arp_file(tmp_path, monkeypatch):
    path = tmp_path / "arp_scan.json"
    monkeypatch.setenv("ARP_SCAN_FILE", str(path))
    import app.provisioning.arrival_watcher as m
    importlib.reload(m)
    return path


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_fresh_helper_snapshot_supplies_arp_data(arp_file):
    _write(arp_file, {
        "schema": 1, "generated_at": int(time.time()),
        "platform": "macos", "tool": "arp",
        "neighbors": [
            {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.42"},
            {"mac": "11:22:33:44:55:66", "ip": "192.168.1.43"},
        ],
        "error": None,
    })
    from app.provisioning import arrival_watcher
    snap = asyncio.run(arrival_watcher._arp_snapshot())
    assert snap.get("aa:bb:cc:dd:ee:ff") == "192.168.1.42"
    assert snap.get("11:22:33:44:55:66") == "192.168.1.43"


def test_stale_snapshot_falls_back_to_in_container_arp(arp_file, monkeypatch):
    _write(arp_file, {
        "schema": 1, "generated_at": int(time.time()) - 200,
        "platform": "macos", "tool": "arp",
        "neighbors": [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.42"}],
        "error": None,
    })
    os.utime(arp_file, (time.time() - 200, time.time() - 200))
    from app.provisioning import arrival_watcher
    # Stub the in-container fallback so the test doesn't depend on the
    # CI host's actual ARP cache. We expect the watcher to drop the
    # stale snapshot and call the real backend, which is now empty.
    monkeypatch.setattr(arrival_watcher, "shutil", arrival_watcher.shutil)  # no-op for clarity
    monkeypatch.setattr(arrival_watcher.sys, "platform", "linux")
    snap = asyncio.run(arrival_watcher._arp_snapshot())
    # Empty because no real ip/arp tooling — but importantly the stale
    # snapshot didn't poison the result.
    assert "aa:bb:cc:dd:ee:ff" not in snap


def test_corrupt_snapshot_is_ignored(arp_file):
    arp_file.write_text("definitely not json {")
    from app.provisioning import arrival_watcher
    # We don't await scan-from-real-arp here; just check the host helper
    # bypass returned None so the caller falls through.
    assert arrival_watcher._host_arp_snapshot() is None


def test_arrival_watcher_fires_when_helper_sees_expected_mac(arp_file):
    _write(arp_file, {
        "schema": 1, "generated_at": int(time.time()),
        "platform": "macos", "tool": "arp",
        "neighbors": [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.7"}],
        "error": None,
    })
    from app.provisioning.arrival_watcher import ArrivalWatcher

    async def _drive():
        w = ArrivalWatcher(poll_interval_s=0.05)
        w.expect("aa:bb:cc:dd:ee:ff")
        async for arrival in w.stream(timeout_s=2.0):
            return arrival
        return None

    arrival = asyncio.run(_drive())
    assert arrival is not None
    assert arrival.mac == "aa:bb:cc:dd:ee:ff"
    assert arrival.ip == "10.0.0.7"


def test_wrong_schema_is_ignored(arp_file):
    _write(arp_file, {
        "schema": 99, "generated_at": int(time.time()),
        "platform": "macos", "tool": "arp",
        "neighbors": [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.7"}],
        "error": None,
    })
    from app.provisioning import arrival_watcher
    assert arrival_watcher._host_arp_snapshot() is None
