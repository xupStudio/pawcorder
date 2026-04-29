"""Tests for the per-container resource snapshot."""
from __future__ import annotations


def test_cpu_percent_handles_first_read(data_dir):
    """First read after start has no precpu_stats — must return 0,
    not crash with KeyError."""
    from app.perf import _cpu_percent
    assert _cpu_percent({}) == 0.0
    assert _cpu_percent({"cpu_stats": {}}) == 0.0


def test_cpu_percent_basic_math(data_dir):
    from app.perf import _cpu_percent
    stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000_000},
            "system_cpu_usage": 10_000_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 500_000_000},
            "system_cpu_usage": 5_000_000_000,
        },
    }
    # cpu_delta=500M, sys_delta=5G, online=4 → 500M/5G * 4 * 100 = 40 %
    assert abs(_cpu_percent(stats) - 40.0) < 0.01


def test_memory_excludes_cache(data_dir):
    from app.perf import _memory
    used, limit = _memory({
        "memory_stats": {
            "usage": 200 * 1024 * 1024,
            "limit": 1024 * 1024 * 1024,
            "stats": {"cache": 50 * 1024 * 1024},
        }
    })
    assert used == 150 * 1024 * 1024
    assert limit == 1024 * 1024 * 1024


def test_network_sums_interfaces(data_dir):
    from app.perf import _network
    rx, tx = _network({
        "networks": {
            "eth0": {"rx_bytes": 1000, "tx_bytes": 200},
            "eth1": {"rx_bytes": 50, "tx_bytes": 30},
        },
    })
    assert rx == 1050
    assert tx == 230


def test_snapshot_one_missing_container(data_dir, monkeypatch):
    """Container not present → returns a record with error set, not crash."""
    from app import docker_ops, perf

    class _Boom:
        class containers:
            @staticmethod
            def get(_):
                raise RuntimeError("not found")
    monkeypatch.setattr(docker_ops, "_client", lambda: _Boom())
    snap = perf.snapshot_one("ghost")
    assert snap.running is False
    assert "missing" in snap.error.lower()


def test_perf_route(authed_client):
    resp = authed_client.get("/api/system/perf")
    assert resp.status_code == 200
    assert "snapshots" in resp.json()
