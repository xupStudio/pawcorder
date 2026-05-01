"""Robust anomaly detection primitives for pet-behaviour baselines.

Pure-stdlib helpers used by ``bowl_monitor`` and ``litter_monitor`` to
replace the original "today's count < 40 % of mean" heuristic. The math
is the **modified z-score** of Iglewicz & Hoaglin (1993):

    z* = 0.6745 × (x - median) / MAD

where ``MAD = median(|x_i - median|)``. The ``0.6745`` constant scales
MAD into the same units as a Gaussian σ — z*=3.5 corresponds to the
classical "3 σ" anomaly cutoff for a normal distribution, but works
even when the input is skewed or has a few outliers (a single bad day
can't move the median, but it can move the mean a lot).

Why not Prophet / IsolationForest?
----------------------------------

Prophet is overkill for daily counts of pet visits — it's tuned for
business seasonality and brings a heavy fbprophet dep. Isolation Forest
is the right primitive when we want **multi-feature** anomaly (e.g.
visit count + duration + hour distribution all considered jointly), and
we'll add it for v2 — but the v1 win comes from making the existing
single-feature detector robust to outliers, not from new ML.

Conformal anomaly detection is the right v3 primitive (gives a
calibrated p-value the UI can map to a sensitivity slider) but needs
more history to calibrate than most users will accumulate in their
first month — premature here.

API
---

Three functions used by the monitors:

  * :func:`robust_score` — modified z-score, returns ``-z*`` so a low
    value (e.g. fewer visits than usual) shows as a *positive* anomaly
    score. Sign convention matches the monitors' "drop = bad".
  * :func:`is_anomaly` — boolean wrapper, default cutoff 3.5 (the
    Iglewicz/Hoaglin recommended threshold).
  * :func:`anomaly_explanation` — short plain-language string for the
    /pets/health UI ("today: 3 visits, usually 8 ± 1.5"). No jargon
    so pet owners don't have to translate.

Test posture: pure functions with zero I/O, so monitor tests can pass
hand-built lists without mocking sightings.

License posture: stdlib only, no new deps. Nothing to license.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# Iglewicz–Hoaglin's recommended anomaly cutoff. z* >= 3.5 is reliably
# unusual under most realistic distributions; pet visit counts in
# particular are roughly Poisson-shaped where MAD-z* is well-calibrated.
DEFAULT_THRESHOLD = 3.5

# The MAD scaling constant — 1 / Φ⁻¹(0.75) — that maps MAD onto the
# same scale as σ for a normal distribution. Pulled out of the formula
# so future readers don't have to rederive it.
MAD_NORMAL_SCALE = 0.6745

# Floor on MAD so a freak streak of identical-count days doesn't yield
# σ = 0 (every value is then "infinite z-score"). 0.5 is a half-visit
# floor — coarser than the natural integer-count granularity, but stops
# false-alarms on quiet pets with very steady routines.
MIN_MAD = 0.5


@dataclass
class AnomalySnapshot:
    """One pet's anomaly state for one metric. The monitors translate
    this into Telegram messages and JSON for the /pets/health page."""
    today: float
    median: float
    mad: float
    score: float           # signed: + means below median, - means above
    is_anomaly: bool
    threshold: float
    n_baseline: int        # how many days of history fed the baseline


def _median(xs: Sequence[float]) -> float:
    """Median over a non-empty sequence. Stdlib statistics is fine but
    we re-implement to avoid a dependency on the order of the input."""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return float(s[n // 2])
    return float((s[n // 2 - 1] + s[n // 2]) / 2.0)


def _baseline_stats(baseline: Sequence[float],
                     *, mad_floor: float = MIN_MAD
                     ) -> tuple[float, float]:
    """Compute (median, MAD) once. The bowl monitor's snapshot path
    and the bare ``robust_score`` helper used to duplicate this math
    — keeping it in one place stops them from drifting."""
    med = _median(baseline)
    deviations = [abs(x - med) for x in baseline]
    mad = max(_median(deviations), mad_floor)
    return med, mad


def robust_score(today: float, baseline: Sequence[float],
                  *, mad_floor: float = MIN_MAD) -> float:
    """Iglewicz–Hoaglin modified z-score, sign-flipped so 'less than
    usual' is a positive anomaly.

    Mathematically: ``z* = 0.6745 × (median - today) / MAD``. Returns
    ``0.0`` when the baseline is too short or degenerate (all-zeros).

    The sign convention is for the monitors' UX — "today is *below* the
    median" maps to a positive score, which we threshold against.
    """
    if not baseline:
        return 0.0
    med, mad = _baseline_stats(baseline, mad_floor=mad_floor)
    if mad <= 0:
        return 0.0
    return MAD_NORMAL_SCALE * (med - today) / mad


def is_anomaly(today: float, baseline: Sequence[float], *,
               threshold: float = DEFAULT_THRESHOLD,
               min_baseline: int = 3) -> bool:
    """Boolean shortcut. Returns False until we have ``min_baseline``
    samples — ramping up a fresh install shouldn't fire on day 2."""
    if len(baseline) < min_baseline:
        return False
    return robust_score(today, baseline) >= threshold


def snapshot(today: float, baseline: Sequence[float], *,
              threshold: float = DEFAULT_THRESHOLD) -> AnomalySnapshot:
    """Bundle the anomaly state — used by the monitors to emit one
    consistent shape into both the JSON API and the Telegram path."""
    if not baseline:
        return AnomalySnapshot(
            today=today, median=0.0, mad=0.0, score=0.0,
            is_anomaly=False, threshold=threshold, n_baseline=0,
        )
    med, mad = _baseline_stats(baseline)
    score = MAD_NORMAL_SCALE * (med - today) / max(mad, 1e-3)
    return AnomalySnapshot(
        today=today, median=med, mad=mad, score=score,
        is_anomaly=score >= threshold,
        threshold=threshold, n_baseline=len(baseline),
    )


# ---- conformal anomaly score (v3) ------------------------------------
#
# The MAD z-score answers "how unusual is today against the central
# tendency of the baseline?". A *conformal* score answers a related but
# different question: "what fraction of past days were as-or-more
# unusual than today?". It's a calibrated p-value the UI can map to a
# sensitivity slider — owners get to pick "tell me only the top 1%" vs
# "tell me anything in the top 20%" without us picking magic constants.
#
# Method: split-conformal with a non-conformity score = |x - median|.
# We compute s_i = |x_i - median| for both today and the historical
# days against the *full-baseline* median. Leave-one-out would give
# slightly tighter calibration on tiny samples, but with ≥14 days a
# single dropped point barely shifts the median, and the (n+1)/(N+1)
# p-value is finite-sample valid under exchangeability either way —
# the simpler version is what we ship.
#
# Why not autoencoders / DL? Production telemetry surveys keep landing
# on conformal as the right primitive when we want a *calibrated*
# false-alarm rate the user can tune. DL anomaly detectors give better
# raw rankings but no calibration story; on this data volume (one row
# per day per pet) DL is also massively over-parameterised.
#
# Limitation: needs MIN_HISTORY days before the p-value is meaningful.
# Below that we return None so the caller falls back to the simpler
# MAD score.

CONFORMAL_MIN_HISTORY = 14   # 14 days = two full weekly cycles


def conformal_p_value(today: float, baseline: Sequence[float]) -> float | None:
    """Finite-sample-valid p-value for "today is at least as unusual
    as the baseline distribution".

    Returns ``None`` when there's not enough history — caller should
    fall back to :func:`robust_score` until calibration data accrues.

    Lower p = more unusual. p < 0.05 corresponds to "top 5% most
    unusual day in the calibration window", which is a sensible default
    sensitivity for a consumer alert.
    """
    if len(baseline) < CONFORMAL_MIN_HISTORY:
        return None
    # Non-conformity score: distance from the full-baseline median.
    # Leave-one-out would tighten calibration slightly on tiny samples
    # but with ≥14 days the sensitivity is below the granularity of an
    # owner-facing chip. (n+1)/(N+1) is finite-sample valid either way.
    full_median = _median(baseline)
    s_today = abs(today - full_median)
    s_baseline = [abs(x - full_median) for x in baseline]
    n_at_or_above = sum(1 for s in s_baseline if s >= s_today)
    return (n_at_or_above + 1) / (len(baseline) + 1)


def conformal_explanation(p_value: float | None, *, units: str = "visits") -> str:
    """Plain-language UI string for the conformal score. Owner-friendly
    rather than statistical — "top 3% of unusual days" not "p < 0.03".
    """
    if p_value is None:
        return ""
    pct = round(p_value * 100)
    if pct <= 1:
        return f"This is in the top 1% of unusual {units} days for your pet."
    if pct <= 5:
        return f"In the top {pct}% of unusual {units} days for your pet."
    if pct <= 20:
        return f"A bit unusual — in the top {pct}% of days."
    return ""


def anomaly_explanation(snap: AnomalySnapshot, *, units: str = "visits") -> str:
    """Plain-language one-liner for the /pets/health page.

    Example outputs:
      "Today 3 visits, usual range about 7-9. Lower than usual."
      "Today 12 visits, usual about 6. Higher than usual."

    The copy is deliberately gentle — pet owners reading the dashboard
    aren't necessarily veterinarians, and an alarmist "ANOMALY" string
    causes more support-bot traffic than it prevents real problems.
    """
    if snap.n_baseline == 0:
        return "Not enough history yet to know what's usual."
    low = max(0.0, snap.median - snap.mad)
    high = snap.median + snap.mad
    today_str = f"{int(round(snap.today))}" if snap.today.is_integer() else f"{snap.today:.1f}"
    base_str = (
        f"about {int(round(snap.median))}"
        if abs(low - high) < 1.0
        else f"about {int(round(low))}-{int(round(high))}"
    )
    if not snap.is_anomaly:
        return f"Today {today_str} {units}, usual is {base_str}."
    direction = "Lower than usual." if snap.score > 0 else "Higher than usual."
    return f"Today {today_str} {units}, usual is {base_str}. {direction}"
