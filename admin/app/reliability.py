"""SLO ledger — record reliability incidents, surface them on /reliability.

The dashboard already showed "is everything OK right now?" via
``health.snapshot()``. This module is the *historical* counterpart:
how often did things break in the last N days, and which subsystem.

Three kinds of events go into the ledger:

  * **camera_offline** — emitted by ``health.HealthMonitor`` when a
    camera misses its frame freshness check, and again on recovery.
    Used to compute per-camera uptime fraction.
  * **ai_inference** — recorded by ``pet_diary`` and ``recognition``
    on every backend call (success OR failure). Failure = upstream
    HTTP error or model unavailable. Used to compute LLM / recognition
    success rate.
  * **push_delivery** — recorded by ``telegram`` / ``webpush`` on
    every notification attempt. Used to compute "did the user actually
    get the alert?" rate.

Storage is NDJSON in ``/data/config/reliability.ndjson`` — same pattern
as the sightings log. Capped at MAX_LOG_LINES so an always-on system
doesn't grow it forever; rotation drops the oldest half.

Why a ledger and not Prometheus / OpenTelemetry: pawcorder is a single-
container appliance. The user already has a browser tab open to the
admin — push them a chart from a file we already write to, instead of
asking them to stand up a monitoring stack for a self-hosted pet camera.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .utils import read_ndjson

logger = logging.getLogger("pawcorder.reliability")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
LEDGER_PATH = DATA_DIR / "config" / "reliability.ndjson"

# Same retention model the sightings log uses — average row is ~150
# bytes, 30k rows ≈ 4.5 MB, plenty for a year's worth of outage events.
MAX_LOG_LINES = 30_000
DEFAULT_WINDOW_DAYS = 7

# Recognised subsystems. Held as a constant so stray strings can't
# create new categories silently — anything outside the set hits a
# warning and lands in the catch-all "other" bucket.
SUBSYSTEMS = ("camera", "ai_inference", "push", "frigate", "storage", "other")

# Outcome flags. "ok" rows are recorded too — without them we couldn't
# tell "10 successes vs. 1 failure" apart from "0 failures, no data".
OUTCOMES = ("ok", "fail", "recovered")

_log_lock = threading.Lock()


@dataclass
class Event:
    """One row in the ledger."""
    ts: float                  # unix seconds
    subsystem: str             # one of SUBSYSTEMS
    name: str                  # camera name, "diary", "telegram", etc
    outcome: str               # one of OUTCOMES
    message: str = ""          # short, user-facing if rendered
    detail: dict | None = None # opaque blob for diagnostics

    def to_dict(self) -> dict:
        out = {
            "ts": self.ts,
            "subsystem": self.subsystem,
            "name": self.name,
            "outcome": self.outcome,
            "message": self.message,
        }
        if self.detail is not None:
            out["detail"] = self.detail
        return out


def _normalised_event(subsystem: str, name: str, outcome: str, *,
                       message: str = "", detail: dict | None = None,
                       ts: Optional[float] = None) -> Event:
    """Validate + normalise inputs into an Event. Shared by record /
    record_batch so we don't duplicate the subsystem / outcome guard."""
    if subsystem not in SUBSYSTEMS:
        logger.warning("reliability: unknown subsystem %r → 'other'", subsystem)
        subsystem = "other"
    if outcome not in OUTCOMES:
        logger.warning("reliability: unknown outcome %r → 'fail'", outcome)
        outcome = "fail"
    return Event(
        ts=ts if ts is not None else time.time(),
        subsystem=subsystem, name=name, outcome=outcome,
        message=message[:200] if message else "",  # cap on disk size
        detail=detail,
    )


def record(subsystem: str, name: str, outcome: str, *,
           message: str = "", detail: dict | None = None,
           ts: Optional[float] = None) -> None:
    """Append one event to the ledger.

    Soft-fails on disk errors — reliability tracking can never break
    the path it's measuring. A stuck disk shouldn't cascade into "the
    LLM diary stops working because we can't write the success log".
    """
    event = _normalised_event(subsystem, name, outcome,
                                message=message, detail=detail, ts=ts)
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with _log_lock:
            with LEDGER_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            _maybe_rotate()
    except OSError as exc:
        logger.warning("reliability ledger write failed: %s", exc)


def record_batch(events: list[tuple]) -> None:
    """Append several events under a single lock + single open.

    `events` is a list of tuples ``(subsystem, name, outcome,
    message, ts)`` — the message and ts trailing fields are optional
    and may be omitted.

    Note: tuples don't carry a ``detail`` field — if a caller wants
    to attach the structured detail blob, fall back to ``record()``
    one-by-one. The batch format is deliberately minimal because
    every current caller (health monitor, future bulk recorders)
    only emits the simple shape.

    Used by the health monitor's per-tick recorder, which writes
    one row per camera plus a frigate row plus a storage row on every
    60 s probe. Pre-batch this was 10+ open/lock/close cycles per
    tick; batch is a single one regardless of camera count.
    """
    if not events:
        return
    normalised: list[Event] = []
    for tup in events:
        sub = tup[0] if len(tup) > 0 else ""
        name = tup[1] if len(tup) > 1 else ""
        outcome = tup[2] if len(tup) > 2 else "fail"
        message = tup[3] if len(tup) > 3 else ""
        ts = tup[4] if len(tup) > 4 else None
        normalised.append(_normalised_event(
            sub, name, outcome, message=message, ts=ts,
        ))
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            with LEDGER_PATH.open("a", encoding="utf-8") as f:
                for e in normalised:
                    f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
            _maybe_rotate()
    except OSError as exc:
        logger.warning("reliability ledger batch-write failed: %s", exc)


def _maybe_rotate() -> None:
    """Drop the oldest half if we're past MAX_LOG_LINES. Same rationale
    as recognition._maybe_rotate."""
    try:
        size = LEDGER_PATH.stat().st_size
    except OSError:
        return
    if size < MAX_LOG_LINES * 200:
        return
    try:
        with LEDGER_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_LOG_LINES:
        return
    keep = lines[-(MAX_LOG_LINES // 2):]
    LEDGER_PATH.write_text("".join(keep), encoding="utf-8")


def read_events(*, since: float = 0.0,
                 subsystem: Optional[str] = None,
                 limit: int = 5000) -> list[dict]:
    """Tail of the ledger filtered by window + optional subsystem."""
    return read_ndjson(
        LEDGER_PATH,
        filter_fn=lambda r: (
            r.get("ts", 0) >= since
            and (subsystem is None or r.get("subsystem") == subsystem)
        ),
        sort_key=lambda r: r.get("ts", 0),
        reverse=True,
        limit=limit,
    )


# ---- summary aggregation ----------------------------------------------


@dataclass
class Slo:
    """One row on the dashboard. ``success_rate`` is a fraction in [0, 1]
    and ``samples`` tells the user how trustworthy the rate is."""
    name: str            # camera name / "diary" / "telegram"
    subsystem: str
    samples: int         # successes + failures
    failures: int
    success_rate: float
    last_failure_ts: Optional[float] = None
    last_failure_msg: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "subsystem": self.subsystem,
            "samples": self.samples,
            "failures": self.failures,
            "success_rate": round(self.success_rate, 4),
            "last_failure_ts": self.last_failure_ts,
            "last_failure_msg": self.last_failure_msg,
        }


def summarize(*, days: int = DEFAULT_WINDOW_DAYS,
               now: Optional[float] = None,
               events: Optional[list[dict]] = None) -> dict:
    """Aggregate the ledger into per-(subsystem, name) success-rate rows.

    Returns a dict shaped for direct serialisation to the /reliability
    page. Tests can pass an explicit `events` list to skip the disk read.
    """
    now = now or time.time()
    since = now - days * 86400
    rows = events if events is not None else read_events(since=since)

    # group_key → {samples, failures, last_fail_ts, last_fail_msg, last_outcome}
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        sub = str(r.get("subsystem") or "other")
        name = str(r.get("name") or "")
        outcome = str(r.get("outcome") or "fail")
        ts = float(r.get("ts") or 0)
        # 'recovered' events count as a success sample — the system was
        # broken, then came back. Without that we'd over-count failures
        # vs. "n minutes of partial uptime".
        if outcome not in OUTCOMES:
            continue
        slot = groups.setdefault((sub, name), {
            "samples": 0, "failures": 0,
            "last_fail_ts": None, "last_fail_msg": "",
        })
        slot["samples"] += 1
        if outcome == "fail":
            slot["failures"] += 1
            if slot["last_fail_ts"] is None or ts > slot["last_fail_ts"]:
                slot["last_fail_ts"] = ts
                slot["last_fail_msg"] = str(r.get("message") or "")[:200]

    out_rows: list[Slo] = []
    for (sub, name), v in groups.items():
        samples = v["samples"]
        failures = v["failures"]
        rate = (samples - failures) / samples if samples > 0 else 1.0
        out_rows.append(Slo(
            name=name, subsystem=sub,
            samples=samples, failures=failures,
            success_rate=rate,
            last_failure_ts=v["last_fail_ts"],
            last_failure_msg=v["last_fail_msg"],
        ))

    # Worst-first: lets the operator's eye land on the broken thing.
    out_rows.sort(key=lambda r: (r.success_rate, -r.samples))

    # Headline number: weighted overall success rate. Cameras with
    # 1000 samples count more than a one-shot failure on a backup job.
    total_samples = sum(r.samples for r in out_rows)
    total_fail = sum(r.failures for r in out_rows)
    overall = (
        (total_samples - total_fail) / total_samples
        if total_samples > 0 else 1.0
    )

    # Subsystem rollup so the page can show one tile per category.
    subsystem_summary: dict[str, dict] = {}
    for r in out_rows:
        s = subsystem_summary.setdefault(r.subsystem, {
            "samples": 0, "failures": 0,
        })
        s["samples"] += r.samples
        s["failures"] += r.failures
    for sub, s in subsystem_summary.items():
        s["success_rate"] = round(
            (s["samples"] - s["failures"]) / s["samples"]
            if s["samples"] > 0 else 1.0,
            4,
        )

    return {
        "window_days": days,
        "now": now,
        "overall_success_rate": round(overall, 4),
        "total_samples": total_samples,
        "total_failures": total_fail,
        "by_subsystem": subsystem_summary,
        "rows": [r.to_dict() for r in out_rows],
    }
