"""Smart insights: cross-pet correlation + energy-mode scheduling.

Two unrelated little features, both pure functions over data we
already have:

  - cross_pet_correlation(): how much time did Mochi and Maru spend
    appearing together (overlapping events on the same camera) over
    the last N hours? Useful for multi-pet households.
  - EnergyMode: schedule of (camera_name, hour_of_day) ranges where
    detection is paused to save power / storage. Used by the Frigate
    template at render time.

Bandwidth monitoring lives in this module too — pulls Frigate's
/api/stats process_fps and turns it into a "kbps estimate" assuming
typical bitrates. Rough but useful for noticing when a camera went
to ultra-high quality and is choking the WAN backup.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import recognition

logger = logging.getLogger("pawcorder.insights")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
ENERGY_PATH = DATA_DIR / "config" / "energy_mode.json"
FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "http://frigate:5000")

# Rough kbps per fps for "typical" home cameras — used by the
# bandwidth estimator. 1080p H.264 at 5 fps is ~600 kbps.
KBPS_PER_FPS_HD = 120


# ---- cross-pet correlation --------------------------------------------

@dataclass
class PairOverlap:
    pet_a: str
    pet_b: str
    overlap_seconds: int
    overlap_count: int            # number of camera-coincident events
    cameras: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pet_a": self.pet_a,
            "pet_b": self.pet_b,
            "overlap_seconds": self.overlap_seconds,
            "overlap_count": self.overlap_count,
            "cameras": list(self.cameras),
        }


def _interval_overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Seconds the two windows overlap."""
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, hi - lo)


def cross_pet_correlation(*, since_hours: float = 24.0) -> list[PairOverlap]:
    """For every distinct pair of pets, sum the overlap of their
    sightings on the SAME camera within the window.

    O(N^2) over events of the same pet — fine at 50 events/day.
    """
    cutoff = time.time() - since_hours * 3600
    rows = [r for r in recognition.read_sightings(limit=10_000, since=cutoff)
            if r.get("pet_id")]
    # Group by pet_id, then by camera.
    by_pet_camera: dict[tuple[str, str], list[dict]] = {}
    pet_names: dict[str, str] = {}
    for r in rows:
        pid = r["pet_id"]
        cam = r.get("camera") or "unknown"
        by_pet_camera.setdefault((pid, cam), []).append(r)
        pet_names[pid] = r.get("pet_name") or pid

    pets = sorted(pet_names.keys())
    out: list[PairOverlap] = []
    for i, a in enumerate(pets):
        for b in pets[i + 1:]:
            total = 0.0
            count = 0
            cams: set = set()
            for (pid_a, cam_a), events_a in by_pet_camera.items():
                if pid_a != a:
                    continue
                events_b = by_pet_camera.get((b, cam_a)) or []
                if not events_b:
                    continue
                for ea in events_a:
                    a_start = float(ea.get("start_time") or 0)
                    a_end = float(ea.get("end_time") or 0) or a_start
                    for eb in events_b:
                        b_start = float(eb.get("start_time") or 0)
                        b_end = float(eb.get("end_time") or 0) or b_start
                        ovr = _interval_overlap(a_start, a_end, b_start, b_end)
                        if ovr > 0:
                            total += ovr
                            count += 1
                            cams.add(cam_a)
            if count > 0:
                out.append(PairOverlap(
                    pet_a=pet_names[a], pet_b=pet_names[b],
                    overlap_seconds=int(total), overlap_count=count,
                    cameras=sorted(cams),
                ))
    return out


# ---- energy mode -------------------------------------------------------

@dataclass
class EnergySchedule:
    """One entry: pause `cameras` from `start_hour` to `end_hour`
    every day. Hours are 0-23 local. end < start means "wrap past
    midnight" (e.g. 22 -> 6 = 22:00-06:00)."""
    cameras: list[str]
    start_hour: int
    end_hour: int

    def to_dict(self) -> dict:
        return {
            "cameras": list(self.cameras),
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
        }


@dataclass
class EnergyMode:
    enabled: bool = False
    schedules: list[EnergySchedule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "schedules": [s.to_dict() for s in self.schedules],
        }


def load_energy_mode() -> EnergyMode:
    if not ENERGY_PATH.exists():
        return EnergyMode()
    try:
        data = json.loads(ENERGY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return EnergyMode()
    schedules = []
    for s in (data.get("schedules") or []):
        if not isinstance(s, dict):
            continue
        schedules.append(EnergySchedule(
            cameras=[str(c) for c in (s.get("cameras") or []) if isinstance(c, str)],
            start_hour=int(s.get("start_hour") or 0),
            end_hour=int(s.get("end_hour") or 0),
        ))
    return EnergyMode(enabled=bool(data.get("enabled", False)), schedules=schedules)


def save_energy_mode(mode: EnergyMode) -> None:
    from .utils import atomic_write_text
    atomic_write_text(ENERGY_PATH, json.dumps(mode.to_dict(), indent=2))


def is_camera_currently_paused(camera_name: str, *, mode: EnergyMode | None = None,
                                hour: int | None = None) -> bool:
    mode = mode if mode is not None else load_energy_mode()
    if not mode.enabled:
        return False
    h = hour if hour is not None else time.localtime().tm_hour
    for s in mode.schedules:
        if camera_name not in s.cameras:
            continue
        sh, eh = s.start_hour, s.end_hour
        if sh == eh:
            continue  # zero-length window, ignore
        if sh < eh:
            # Single contiguous window inside one day.
            if sh <= h < eh:
                return True
        else:
            # Wraps midnight, e.g. 22 -> 6.
            if h >= sh or h < eh:
                return True
    return False


# ---- bandwidth estimator ----------------------------------------------

@dataclass
class CameraBandwidth:
    camera: str
    process_fps: float
    detection_fps: float
    estimated_kbps: float
    online: bool

    def to_dict(self) -> dict:
        return {
            "camera": self.camera,
            "process_fps": round(self.process_fps, 1),
            "detection_fps": round(self.detection_fps, 1),
            "estimated_kbps": round(self.estimated_kbps, 0),
            "online": self.online,
        }


async def bandwidth_per_camera() -> list[CameraBandwidth]:
    """Pulls /api/stats from Frigate, computes a kbps estimate.

    The number is approximate — we don't have actual byte counters
    per stream — but it's accurate enough to spot "this camera is
    streaming 4 Mbps because someone left it on max quality"."""
    out: list[CameraBandwidth] = []
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{FRIGATE_BASE_URL}/api/stats")
        if resp.status_code != 200:
            return out
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return out

    cams = data.get("cameras") or {}
    for name, stats in cams.items():
        if not isinstance(stats, dict):
            continue
        proc = float(stats.get("process_fps") or 0)
        det = float(stats.get("detection_fps") or 0)
        est = (proc + det) * KBPS_PER_FPS_HD / 2  # avg of the two streams
        online = proc > 0 or det > 0
        out.append(CameraBandwidth(
            camera=name, process_fps=proc, detection_fps=det,
            estimated_kbps=est, online=online,
        ))
    return out
