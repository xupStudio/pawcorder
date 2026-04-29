"""Per-container CPU / memory snapshot via the Docker SDK.

`docker stats --no-stream` is what users run from the shell; the
SDK's `container.stats(stream=False)` returns the same numbers as a
nested dict that we flatten into something the UI can render.

Cheap to compute (one Docker API call per container, ~50 ms total
for our three) and cheap on resources (no agent, no Prometheus, no
extra container). Polled on demand from /system; never auto-runs in
the background.

Soft-fails the same way our other Docker callers do — if the socket
isn't mounted, we just return an empty list rather than crash.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import docker_ops

logger = logging.getLogger("pawcorder.perf")

_TARGET_CONTAINERS = ("pawcorder-admin", "pawcorder-frigate", "pawcorder-watchtower")


@dataclass
class PerfSnapshot:
    """One row per container."""
    name: str
    running: bool
    cpu_percent: float = 0.0      # 0..100, summed across all CPUs
    memory_used_bytes: int = 0
    memory_limit_bytes: int = 0
    memory_percent: float = 0.0
    network_rx_bytes: int = 0
    network_tx_bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "running": self.running,
            "cpu_percent": round(self.cpu_percent, 1),
            "memory_used_bytes": self.memory_used_bytes,
            "memory_limit_bytes": self.memory_limit_bytes,
            "memory_percent": round(self.memory_percent, 1),
            "network_rx_bytes": self.network_rx_bytes,
            "network_tx_bytes": self.network_tx_bytes,
            "error": self.error,
        }


def _cpu_percent(stats: dict) -> float:
    """Reproduces the formula `docker stats` uses internally.

    cpu_delta / system_delta * online_cpus * 100. Returns 0 when any
    field is missing (first read after start, or older Docker versions
    that omit some keys).
    """
    cpu = stats.get("cpu_stats") or {}
    pre = stats.get("precpu_stats") or {}
    cpu_total = (cpu.get("cpu_usage") or {}).get("total_usage", 0) or 0
    pre_total = (pre.get("cpu_usage") or {}).get("total_usage", 0) or 0
    cpu_delta = cpu_total - pre_total
    sys_total = cpu.get("system_cpu_usage", 0) or 0
    sys_pre = pre.get("system_cpu_usage", 0) or 0
    sys_delta = sys_total - sys_pre
    online = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    if sys_delta <= 0 or cpu_delta <= 0:
        return 0.0
    return (cpu_delta / sys_delta) * online * 100.0


def _memory(stats: dict) -> tuple[int, int]:
    """Returns (used_bytes, limit_bytes). 'cache' is excluded from used —
    Docker treats page cache as available, the user does too."""
    mem = stats.get("memory_stats") or {}
    usage = int(mem.get("usage") or 0)
    cache = int((mem.get("stats") or {}).get("cache") or 0)
    used = max(0, usage - cache)
    limit = int(mem.get("limit") or 0)
    return used, limit


def _network(stats: dict) -> tuple[int, int]:
    """Total rx/tx across all interfaces. Snapshot — caller computes
    deltas over time if they want bandwidth."""
    nets = stats.get("networks") or {}
    rx = sum(int((n or {}).get("rx_bytes") or 0) for n in nets.values())
    tx = sum(int((n or {}).get("tx_bytes") or 0) for n in nets.values())
    return rx, tx


def snapshot_one(container_name: str) -> PerfSnapshot:
    snap = PerfSnapshot(name=container_name, running=False)
    try:
        client = docker_ops._client()  # noqa: SLF001
        c = client.containers.get(container_name)
    except Exception as exc:  # noqa: BLE001
        snap.error = f"container missing: {exc}"
        return snap
    state = c.attrs.get("State") or {}
    snap.running = bool(state.get("Running"))
    if not snap.running:
        return snap
    try:
        stats = c.stats(stream=False)
    except Exception as exc:  # noqa: BLE001
        snap.error = f"stats failed: {exc}"
        return snap
    snap.cpu_percent = _cpu_percent(stats)
    used, limit = _memory(stats)
    snap.memory_used_bytes = used
    snap.memory_limit_bytes = limit
    snap.memory_percent = (used / limit * 100.0) if limit > 0 else 0.0
    rx, tx = _network(stats)
    snap.network_rx_bytes = rx
    snap.network_tx_bytes = tx
    return snap


def snapshot_all() -> list[PerfSnapshot]:
    return [snapshot_one(n) for n in _TARGET_CONTAINERS]
