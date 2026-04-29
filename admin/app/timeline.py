"""Cross-camera journey timeline for one pet.

Reads sightings.ndjson and stitches sequential events into "journeys":
  - 14:32-14:35 living_room (Mochi, high confidence)
  - 14:36-14:38 hallway      (Mochi, tentative)   ← stitched into same journey
  - 14:39-14:50 kitchen      (Mochi, high)        ← still same journey

The stitch threshold is small (default 90 s) so a real "left scene then
came back later" appears as two journeys, not one. Tunable per call.

Why not just rely on Frigate's grouping: Frigate doesn't know which
events belong to the same pet — that's our identification's job. We
need to group AFTER our pet_id has been assigned.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import recognition

# Two consecutive events on different cameras within this many seconds
# are treated as one continuous "journey" (the pet walked from A to B).
DEFAULT_STITCH_GAP_SECONDS = 90.0
# Hard ceiling on how far back the timeline goes by default.
DEFAULT_LOOKBACK_HOURS = 48.0


@dataclass
class JourneyLeg:
    """One continuous appearance on one camera."""
    camera: str
    start_time: float
    end_time: float
    confidence: str  # 'high' | 'tentative'

    def to_dict(self) -> dict:
        return {
            "camera": self.camera,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "confidence": self.confidence,
        }


@dataclass
class Journey:
    """Several legs that belong to the same pet, close in time."""
    pet_id: str
    pet_name: str
    legs: list[JourneyLeg]

    @property
    def start_time(self) -> float:
        return self.legs[0].start_time if self.legs else 0.0

    @property
    def end_time(self) -> float:
        return self.legs[-1].end_time if self.legs else 0.0

    def to_dict(self) -> dict:
        return {
            "pet_id": self.pet_id,
            "pet_name": self.pet_name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "legs": [l.to_dict() for l in self.legs],
        }


def journeys_for_pet(pet_id: str, *,
                     since_hours: float = DEFAULT_LOOKBACK_HOURS,
                     stitch_gap: float = DEFAULT_STITCH_GAP_SECONDS,
                     limit: int = 200) -> list[Journey]:
    """Return ordered journeys (newest first) for one pet.

    `tentative` confidence sightings are included — UI can dim them.
    `unknown` (no pet_id) sightings are filtered out by definition.
    """
    import time
    cutoff = time.time() - since_hours * 3600
    rows = [r for r in recognition.read_sightings(limit=10_000, since=cutoff)
            if r.get("pet_id") == pet_id]
    if not rows:
        return []

    # Walk in chronological order so we can stitch sequentially.
    rows.sort(key=lambda r: r.get("start_time", 0))
    legs: list[JourneyLeg] = []
    journeys: list[Journey] = []
    pet_name = next((r.get("pet_name") for r in rows if r.get("pet_name")), pet_id)

    def flush() -> None:
        if legs:
            journeys.append(Journey(pet_id=pet_id, pet_name=pet_name, legs=list(legs)))
            legs.clear()

    last_end = -1.0
    for r in rows:
        leg = JourneyLeg(
            camera=str(r.get("camera") or "unknown"),
            start_time=float(r.get("start_time") or 0),
            end_time=float(r.get("end_time") or 0) or float(r.get("start_time") or 0),
            confidence=str(r.get("confidence") or "tentative"),
        )
        if last_end > 0 and leg.start_time - last_end > stitch_gap:
            flush()
        legs.append(leg)
        last_end = leg.end_time
    flush()

    journeys.sort(key=lambda j: j.start_time, reverse=True)
    return journeys[:limit]


def cross_camera_summary(*, since_hours: float = 24.0) -> dict[str, dict]:
    """For the /pets list page header: per-pet count and last-seen.

    Returns {pet_id: {sightings, last_seen, cameras}} for every pet
    seen in the window. Pets with zero sightings are NOT in the output —
    callers can detect that and show "—" or "未見過".
    """
    import time
    cutoff = time.time() - since_hours * 3600
    rows = [r for r in recognition.read_sightings(limit=10_000, since=cutoff)
            if r.get("pet_id")]
    summary: dict[str, dict] = {}
    for r in rows:
        pid = r["pet_id"]
        bucket = summary.setdefault(pid, {
            "pet_id": pid,
            "pet_name": r.get("pet_name") or pid,
            "sightings": 0,
            "last_seen": 0.0,
            "cameras": set(),
        })
        bucket["sightings"] += 1
        ts = float(r.get("start_time") or 0)
        if ts > bucket["last_seen"]:
            bucket["last_seen"] = ts
        if r.get("camera"):
            bucket["cameras"].add(r["camera"])
    # Convert sets → sorted lists for JSON.
    for v in summary.values():
        v["cameras"] = sorted(v["cameras"])
    return summary
