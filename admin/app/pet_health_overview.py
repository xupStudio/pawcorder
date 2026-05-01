"""Aggregator for the per-pet health overview page.

Pulls every health signal we have for a given pet into one bundle that
the ``/pets/health`` template renders as charts. This is OSS — it
works with whichever subset of Pro detectors is installed:

  * ``app.pro.pet_health``        — absence + activity anomaly
  * ``app.pro.litter_monitor``    — UTI early warning
  * ``app.pro.bowl_monitor``      — water + food bowl trends
  * ``app.pro.fight_detector``    — co-sighting clusters
  * ``app.pro.posture_detector``  — vomit / gait flags

Each signal contributes a slice of the daily timeline and a slice of
the composite "health score" 0–100 we surface as a dial. Missing Pro
modules just don't contribute — the OSS-only view shows
30-day activity, hour×weekday heatmap, and the system uptime ribbon.

Why aggregate here vs. extend ``/api/pets/health``:

  * The existing API is per-snapshot (today + 24 h windows). The
    overview page wants 30-day timelines and per-day buckets — a
    different shape, computed from the same sightings log but bucket-
    grained. Keeping the two endpoints separate avoids a polymorphic
    response that some screens use 5 % of and others use 95 % of.
  * Chart data lives server-rendered in this module so the SVG
    helpers can produce strings the Jinja template just drops in. No
    JS chart-data parsing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import recognition, reliability, svg_charts
from .pets_store import Pet, PetStore

# Pro detectors: optional. Resolve to None on OSS builds.
try:
    from .pro import pet_health  # type: ignore[attr-defined]
except ImportError:
    pet_health = None  # type: ignore[assignment]
try:
    from .pro import litter_monitor  # type: ignore[attr-defined]
except ImportError:
    litter_monitor = None  # type: ignore[assignment]
try:
    from .pro import bowl_monitor  # type: ignore[attr-defined]
except ImportError:
    bowl_monitor = None  # type: ignore[assignment]
try:
    from .pro import fight_detector  # type: ignore[attr-defined]
except ImportError:
    fight_detector = None  # type: ignore[assignment]
try:
    from .pro import posture_detector  # type: ignore[attr-defined]
except ImportError:
    posture_detector = None  # type: ignore[assignment]

logger = logging.getLogger("pawcorder.pet_health_overview")

OVERVIEW_DAYS = 30
HEATMAP_DAYS = 14    # smaller window so a stable pattern is visible


# ---- one pet's overview ------------------------------------------------

@dataclass
class TimelineDay:
    """Per-day total + per-camera breakdown for a single pet."""
    date: str
    total: int = 0
    by_camera: dict[str, int] = field(default_factory=dict)
    # Hour-bucketed counts so the per-day card can flag "Mochi was up
    # at 4 AM" without re-scanning the raw log.
    by_hour: list[int] = field(default_factory=lambda: [0] * 24)


@dataclass
class PetOverview:
    """One pet's full overview. Values pre-rendered to SVG strings so
    the template just drops them in via ``| safe``."""
    pet_id: str
    pet_name: str
    species: str

    # Composite score 0–100. Higher is healthier.
    score: float = 100.0
    score_label: str = "ok"            # "ok" | "watch" | "alert"
    score_reasons: list[str] = field(default_factory=list)

    # 30-day timeline (newest day last so the chart reads left → right).
    timeline_days: list[TimelineDay] = field(default_factory=list)

    # Pre-rendered SVG fragments. Empty string when the underlying
    # signal has no data (template hides those cards).
    activity_chart_svg: str = ""
    heatmap_svg: str = ""
    score_dial_svg: str = ""
    sparkline_litter_svg: str = ""
    sparkline_water_svg: str = ""
    sparkline_food_svg: str = ""

    # Raw flags lifted from the per-detector snapshots — surfaced as
    # tile chips on the page header.
    absence_anomaly: bool = False
    activity_anomaly: bool = False
    litter_frequent: bool = False
    litter_phantom: bool = False
    bowl_drops: list[str] = field(default_factory=list)        # ["water_bowl", ...]
    bowl_silent: list[str] = field(default_factory=list)
    posture_flags: list[str] = field(default_factory=list)     # ["vomit", "gait"]
    fight_pairs: list[str] = field(default_factory=list)       # ["Mochi & Maru", ...]
    # Plain-language explanations from the robust-MAD anomaly module —
    # one entry per (bowl_kind, message). Surfaced on the /pets/health
    # page beside the sparklines so the owner sees concrete numbers
    # rather than just "🟠 anomaly" badges.
    bowl_explanations: list[dict] = field(default_factory=list)
    # Multi-frame recognition health: how many of today's sightings
    # benefited from quality-weighted multi-frame matching. Used to show
    # an "improved accuracy" line on /pets/health.
    multi_frame_today: int = 0
    sightings_today: int = 0
    # Today's behavior label distribution (resting / pacing / active /
    # idle). One ``primary`` label drives the chip; counts feed the
    # tooltip / explanation. Empty when the pet had no events today.
    behavior: dict = field(default_factory=dict)


# ---- chart-data computation -------------------------------------------

def _build_timeline(rows: list[dict], pet_id: str, *, now: float,
                     days: int) -> list[TimelineDay]:
    """Bucket rows for one pet into local-day TimelineDays.

    Returns ``days`` entries, oldest first. A day with zero sightings
    still appears (the chart wants the gap visible)."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("pet_id") != pet_id:
            continue
        ts = float(r.get("start_time") or 0)
        if ts <= 0:
            continue
        key = time.strftime("%Y-%m-%d", time.localtime(ts))
        by_day.setdefault(key, []).append(r)

    out: list[TimelineDay] = []
    for offset in range(days - 1, -1, -1):
        ts = now - offset * 86400
        key = time.strftime("%Y-%m-%d", time.localtime(ts))
        td = TimelineDay(date=key)
        for r in by_day.get(key, []):
            td.total += 1
            cam = str(r.get("camera") or "?")
            td.by_camera[cam] = td.by_camera.get(cam, 0) + 1
            t = float(r.get("start_time") or 0)
            if t > 0:
                td.by_hour[time.localtime(t).tm_hour] += 1
        out.append(td)
    return out


def _build_heatmap(rows: list[dict], pet_id: str, *, now: float,
                    days: int) -> list[list[int]]:
    """Hour-of-day × day-of-week intensity grid (24 cols × 7 rows).

    Aggregates ``days`` worth of recent sightings; rows[0] = Monday.
    Owners read this to spot rhythm changes ("Mochi used to be active
    Tuesday evenings, now she's quiet there")."""
    grid = [[0] * 24 for _ in range(7)]
    cutoff = now - days * 86400
    for r in rows:
        if r.get("pet_id") != pet_id:
            continue
        ts = float(r.get("start_time") or 0)
        if ts < cutoff or ts <= 0:
            continue
        t = time.localtime(ts)
        # tm_wday: Monday is 0. Matches our intended row order.
        grid[t.tm_wday][t.tm_hour] += 1
    return grid


def _series_per_camera(timeline: list[TimelineDay], top_n: int = 3
                        ) -> list[tuple[str, list[float], str]]:
    """Pick the top-N cameras by total visits across the window, return
    each as a ``(name, daily_counts, color)`` tuple suitable for
    :func:`svg_charts.stacked_bars`. The "Other" bucket aggregates the
    long tail so a 9-camera install doesn't produce a confetti chart."""
    totals: dict[str, int] = {}
    for d in timeline:
        for cam, c in d.by_camera.items():
            totals[cam] = totals.get(cam, 0) + c
    top = sorted(totals.items(), key=lambda kv: -kv[1])[:top_n]
    top_names = [n for n, _ in top]
    palette = [
        svg_charts.COLOR_BRAND,
        "#0ea5e9",   # sky-500
        "#10b981",   # emerald-500
    ]
    series: list[tuple[str, list[float], str]] = []
    for i, (name, _) in enumerate(top):
        daily = [float(d.by_camera.get(name, 0)) for d in timeline]
        series.append((name, daily, palette[i % len(palette)]))
    other_daily = [
        float(sum(c for cam, c in d.by_camera.items() if cam not in top_names))
        for d in timeline
    ]
    if any(other_daily):
        series.append(("Other", other_daily, svg_charts.COLOR_MUTED))
    return series


def _bowl_visits_by_day(rows: list[dict], pet_id: str, *,
                         bowls: list, kind: str,
                         now: float, days: int) -> list[int]:
    """For one bowl kind, count daily pet visits across the window.
    Caller passes the pre-discovered bowl list (to avoid an O(pets)
    re-scan of cameras.yml on every page render)."""
    relevant = [b for b in bowls if b.kind == kind]
    if not relevant or bowl_monitor is None:
        return [0] * days

    def _hits_any_bowl(r: dict) -> bool:
        return any(bowl_monitor._row_in_bowl(r, b) for b in relevant)

    return recognition.daily_buckets(
        rows, pet_id=pet_id, predicate=_hits_any_bowl,
        now=now, days=days,
    )


def _litter_visits_by_day(rows: list[dict], pet_id: str, *,
                           now: float, days: int,
                           camera: str, zone: dict | None) -> list[int]:
    """Daily litter-box visit counts via the same predicate the live
    alerter uses (single source of truth for "is this a litter visit")."""
    if litter_monitor is None or not camera:
        return [0] * days
    return recognition.daily_buckets(
        rows, pet_id=pet_id,
        predicate=litter_monitor.litter_visit_predicate(camera, zone),
        now=now, days=days,
    )


# ---- score computation -------------------------------------------------

def _score_pet(p: PetOverview) -> tuple[float, str, list[str]]:
    """Composite score with reason strings the page shows in a tooltip.

    Weights picked so a single anomaly takes the score under 80 (amber)
    but multiple are needed to reach < 60 (red). The numbers are
    deliberately coarse — owners read colour, not decimal points.
    """
    score = 100.0
    reasons: list[str] = []
    if p.absence_anomaly:
        score -= 25
        reasons.append("not_seen")
    if p.activity_anomaly:
        score -= 15
        reasons.append("activity_low")
    if p.litter_frequent:
        score -= 25
        reasons.append("litter_frequent")
    if p.litter_phantom:
        score -= 25
        reasons.append("litter_phantom")
    for _ in p.bowl_drops:
        score -= 12
        reasons.append("bowl_drop")
    for _ in p.bowl_silent:
        score -= 18
        reasons.append("bowl_silent")
    for _ in p.posture_flags:
        score -= 10
        reasons.append("posture")
    score = max(0.0, score)
    if score >= 80:
        label = "ok"
    elif score >= 60:
        label = "watch"
    else:
        label = "alert"
    # Dedupe reason chips so an owner with both food + water drops sees
    # one "bowl_drop" chip, not two redundant ones. Score deduction
    # already happened above per-occurrence — only the chip list dedupes.
    return score, label, list(dict.fromkeys(reasons))


# ---- public entry point ------------------------------------------------

def overview_for_all_pets(*, now: Optional[float] = None,
                            lang: str = "en") -> list[PetOverview]:
    """Build one PetOverview per configured pet. Empty list if no pets
    or no recognition history yet — the page renders an empty-state.

    ``lang`` flows through to the behavior-chip explanation so the chip
    renders in the same locale as the rest of the page (zh-TW / en).

    All charts are pre-rendered to SVG strings here. Callers (the
    template) should never need to know about chart internals.
    """
    pets = PetStore().load()
    if not pets:
        return []
    now = now or time.time()
    rows = recognition.read_sightings(
        limit=50_000,
        since=now - (OVERVIEW_DAYS + 1) * 86400,
    )

    # Discover bowls + resolve litter zone ONCE per page render — these
    # parse cameras.yml from disk; calling them per-pet was O(pets) reads.
    bowls = bowl_monitor._discover_bowls() if bowl_monitor is not None else []
    from . import config_store
    cfg = config_store.load_config()
    litter_camera = cfg.litter_box_camera
    litter_zone = (litter_monitor._resolve_litter_zone(litter_camera)
                   if (litter_monitor is not None and litter_camera) else None)

    # Snap detector outputs once — a 30-min poll is fine but we want
    # the page to read consistent values relative to ``now``.
    pet_h = (pet_health.snapshots_all(now=now, rows=rows)
             if pet_health is not None else [])
    pet_h_by_id = {s.pet_id: s for s in pet_h}

    litter_h = (litter_monitor.snapshots_all(now=now, rows=rows)
                if litter_monitor is not None else [])
    litter_by_id = {s.pet_id: s for s in litter_h}

    bowl_h = (bowl_monitor.snapshots_all(now=now, rows=rows)
              if bowl_monitor is not None else [])
    bowl_by_pet: dict[str, list] = {}
    for s in bowl_h:
        bowl_by_pet.setdefault(s.pet_id, []).append(s)

    posture_h = []
    if posture_detector is not None:
        posture_h.extend(posture_detector.vomit_snapshots(now=now, rows=rows))
        posture_h.extend(posture_detector.gait_snapshots(now=now, rows=rows))
    posture_by_pet: dict[str, list[str]] = {}
    for s in posture_h:
        if s.flagged:
            posture_by_pet.setdefault(s.pet_id, []).append(s.kind)

    fight_clusters = (fight_detector._scan_clusters(rows)
                      if fight_detector is not None else [])
    fights_by_pet: dict[str, list[str]] = {}
    for c in fight_clusters:
        pair = f"{c.pet_a_name} & {c.pet_b_name}"
        fights_by_pet.setdefault(c.pet_a_id, []).append(pair)
        fights_by_pet.setdefault(c.pet_b_id, []).append(pair)

    out: list[PetOverview] = []
    for pet in pets:
        ov = _build_one(
            pet, rows=rows, now=now,
            pet_h_snap=pet_h_by_id.get(pet.pet_id),
            litter_snap=litter_by_id.get(pet.pet_id),
            bowl_snaps=bowl_by_pet.get(pet.pet_id, []),
            posture_kinds=posture_by_pet.get(pet.pet_id, []),
            fight_pairs=fights_by_pet.get(pet.pet_id, []),
            bowls=bowls,
            litter_camera=litter_camera,
            litter_zone=litter_zone,
            lang=lang,
        )
        out.append(ov)
    return out


def _build_one(pet: Pet, *, rows: list[dict], now: float,
                pet_h_snap, litter_snap, bowl_snaps,
                posture_kinds: list[str],
                fight_pairs: list[str],
                bowls: list,
                litter_camera: str,
                litter_zone: dict | None,
                lang: str = "en") -> PetOverview:
    ov = PetOverview(pet_id=pet.pet_id, pet_name=pet.name, species=pet.species)
    ov.timeline_days = _build_timeline(rows, pet.pet_id, now=now,
                                         days=OVERVIEW_DAYS)

    # Activity stacked bars — only render when there's something to
    # show. Rendering an empty SVG looks like a bug.
    if any(d.total for d in ov.timeline_days):
        labels = [d.date[5:] for d in ov.timeline_days]   # MM-DD
        series = _series_per_camera(ov.timeline_days)
        # Thin labels so 30 days don't overlap.
        ov.activity_chart_svg = svg_charts.stacked_bars(
            labels, series, max_label_every=5, height=200,
        )
    # Heatmap — last HEATMAP_DAYS to keep the pattern legible.
    grid = _build_heatmap(rows, pet.pet_id, now=now, days=HEATMAP_DAYS)
    if any(any(row) for row in grid):
        days_label = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        ov.heatmap_svg = svg_charts.heatmap_grid(
            grid,
            row_labels=days_label,
            col_labels=[str(h) for h in range(24)],
        )

    # Bowl + litter sparklines.
    if litter_snap is not None and litter_camera:
        counts = _litter_visits_by_day(
            rows, pet.pet_id, now=now, days=14,
            camera=litter_camera, zone=litter_zone,
        )
        if any(counts):
            ov.sparkline_litter_svg = svg_charts.sparkline(
                counts, stroke=svg_charts.COLOR_WARN, label_last=True,
                width=160, height=36,
            )
    if bowls:
        water = _bowl_visits_by_day(rows, pet.pet_id, bowls=bowls,
                                     kind="water_bowl",
                                     now=now, days=14)
        if any(water):
            ov.sparkline_water_svg = svg_charts.sparkline(
                water, stroke="#0ea5e9", label_last=True,
                width=160, height=36,
            )
        food = _bowl_visits_by_day(rows, pet.pet_id, bowls=bowls,
                                    kind="food_bowl",
                                    now=now, days=14)
        if any(food):
            ov.sparkline_food_svg = svg_charts.sparkline(
                food, stroke="#10b981", label_last=True,
                width=160, height=36,
            )

    # Lift detector flags.
    if pet_h_snap is not None:
        ov.absence_anomaly = pet_h_snap.absence_anomaly
        ov.activity_anomaly = pet_h_snap.activity_anomaly
    if litter_snap is not None:
        ov.litter_frequent = litter_snap.frequent
        ov.litter_phantom = litter_snap.phantom_run
    for s in bowl_snaps:
        if s.drop_anomaly:
            ov.bowl_drops.append(s.bowl_kind)
        if s.silent_day:
            ov.bowl_silent.append(s.bowl_kind)
        # Always surface the explanation, even when not anomalous —
        # the page shows it as a quiet "today 5 visits, usual 4-6"
        # so the owner gets context, not just alerts.
        if getattr(s, "explanation", ""):
            ov.bowl_explanations.append({
                "bowl_kind": s.bowl_kind,
                "explanation": s.explanation,
                "is_anomaly": bool(s.drop_anomaly),
            })
    ov.posture_flags = list(posture_kinds)
    # De-dupe fight-pair strings (one pair can appear in multiple clusters).
    ov.fight_pairs = list(dict.fromkeys(fight_pairs))

    # Multi-frame recognition stats for today (local day). Cheap — we
    # already loaded these rows above for activity charts.
    today_label = time.strftime("%Y-%m-%d", time.localtime(now))
    multi = 0
    total = 0
    for r in rows:
        if r.get("pet_id") != pet.pet_id:
            continue
        ts = float(r.get("start_time") or 0)
        if ts <= 0:
            continue
        if time.strftime("%Y-%m-%d", time.localtime(ts)) != today_label:
            continue
        total += 1
        if int(r.get("frames_used") or 1) > 1:
            multi += 1
    ov.sightings_today = total
    ov.multi_frame_today = multi

    # Behavior label distribution for today. Cheap — runs over the
    # already-loaded ``rows`` slice. Returns empty when no events fall
    # in today, which the template treats as "hide the chip".
    from . import behavior
    bsum = behavior.day_summary(pet.pet_id, pet.name, events=rows, now=now)
    if bsum.total_events > 0:
        ov.behavior = bsum.to_dict()
        # Plain-language explanation surfaces alongside other chips —
        # only included for non-idle primary so we don't dilute the
        # page with "today: idle" rows that say nothing. Lang is
        # passed through so the chip text matches the rest of the page
        # (zh-TW / en); falls back to en when the caller didn't set it.
        if bsum.primary != "idle":
            msg = behavior.label_explanation(
                bsum.primary, bsum.counts.get(bsum.primary, 0),
                lang=lang or "en",
            )
            if msg:
                ov.behavior["explanation"] = msg

    score, label, reasons = _score_pet(ov)
    ov.score = score
    ov.score_label = label
    ov.score_reasons = reasons
    ov.score_dial_svg = svg_charts.health_score_dial(score, size=78)
    return ov


# ---- system ribbon -----------------------------------------------------

def system_uptime_ribbon(*, days: int = 7) -> str:
    """Per-day overall-up tri-state for the page header.

    Reads the reliability ledger; a day with samples is green when
    >=99% of them were OK, red otherwise. A day with **zero** samples
    is grey — important: a deploy that silently breaks the reliability
    writer would otherwise paint full-green and the owner would trust
    it. Grey days nudge "I don't know" instead of "all good".

    Exception: a brand-new install (ledger missing entirely, never
    written) is treated as grey across the board; we don't pretend a
    fresh install is healthy.
    """
    now = time.time()
    ledger_exists = reliability.LEDGER_PATH.exists()
    try:
        events = reliability.read_events(since=now - days * 86400, limit=20_000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("reliability.read_events failed: %s", exc)
        events = []
    by_day: dict[str, list[int]] = {}
    for r in events:
        ts = float(r.get("ts") or 0)
        if ts <= 0:
            continue
        key = time.strftime("%Y-%m-%d", time.localtime(ts))
        slot = by_day.setdefault(key, [0, 0])
        if r.get("outcome") == "ok":
            slot[0] += 1
        elif r.get("outcome") == "fail":
            slot[1] += 1
    per_day: list = []
    titles: list[str] = []
    for offset in range(days - 1, -1, -1):
        ts = now - offset * 86400
        key = time.strftime("%Y-%m-%d", time.localtime(ts))
        ok, fail = by_day.get(key, [0, 0])
        if ok + fail == 0 or not ledger_exists:
            per_day.append(None)   # tri-state: grey "no data"
            titles.append(time.strftime("%a", time.localtime(ts)) + " · no data")
        else:
            up = ok / (ok + fail) >= 0.99
            per_day.append(up)
            titles.append(
                time.strftime("%a", time.localtime(ts))
                + f" · {ok}/{ok + fail}"
            )
    return svg_charts.uptime_ribbon(per_day, title_each=titles, width=240)
