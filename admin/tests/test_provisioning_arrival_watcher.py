"""Tests for the LAN-arrival watcher.

We monkeypatch ``_arp_snapshot`` so we don't shell out to the host's
``ip neigh`` / ``arp`` during tests. The watcher's logic — match an
expected MAC against a snapshot, fire once, and exit when there's
nothing left to wait on — is what we want to verify.
"""
from __future__ import annotations

import asyncio

import pytest


def test_arrival_fires_once_when_mac_appears(monkeypatch, data_dir):
    from app.provisioning import arrival_watcher

    snapshots = iter(
        [
            {},  # nothing yet
            {"aa:bb:cc:dd:ee:ff": "192.168.1.42"},  # appears
        ]
    )

    async def fake_snap():
        try:
            return next(snapshots)
        except StopIteration:
            return {}

    monkeypatch.setattr(arrival_watcher, "_arp_snapshot", fake_snap)

    async def run():
        watcher = arrival_watcher.ArrivalWatcher(poll_interval_s=0.01)
        watcher.expect("aa:bb:cc:dd:ee:ff")
        events = []
        async for arrival in watcher.stream(timeout_s=2.0):
            events.append(arrival)
        return events

    events = asyncio.run(run())
    assert len(events) == 1
    assert events[0].ip == "192.168.1.42"
    assert events[0].mac == "aa:bb:cc:dd:ee:ff"


def test_arrival_normalises_mac(monkeypatch, data_dir):
    """Caller can pass an unusual MAC format and the watcher matches."""
    from app.provisioning import arrival_watcher

    async def fake_snap():
        return {"aa:bb:cc:dd:ee:ff": "10.0.0.1"}

    monkeypatch.setattr(arrival_watcher, "_arp_snapshot", fake_snap)

    async def run():
        watcher = arrival_watcher.ArrivalWatcher(poll_interval_s=0.01)
        watcher.expect("AA-BB-CC-DD-EE-FF")  # dash-separated upper case
        async for arrival in watcher.stream(timeout_s=1.0):
            return arrival
        return None

    arrival = asyncio.run(run())
    assert arrival is not None
    assert arrival.ip == "10.0.0.1"


def test_arrival_times_out_with_no_match(monkeypatch, data_dir):
    from app.provisioning import arrival_watcher

    async def fake_snap():
        return {"99:99:99:99:99:99": "10.0.0.99"}  # always wrong MAC

    monkeypatch.setattr(arrival_watcher, "_arp_snapshot", fake_snap)

    async def run():
        watcher = arrival_watcher.ArrivalWatcher(poll_interval_s=0.01)
        watcher.expect("aa:bb:cc:dd:ee:ff")
        events = []
        async for arrival in watcher.stream(timeout_s=0.05):
            events.append(arrival)
        return events

    events = asyncio.run(run())
    assert events == []


def test_arrival_ignores_zero_mac(monkeypatch, data_dir):
    """ARP cache often has 0-MAC entries for unresolved IPs."""
    from app.provisioning import arrival_watcher

    watcher = arrival_watcher.ArrivalWatcher()
    watcher.expect("00:00:00:00:00:00")
    assert watcher.expected_macs == []
