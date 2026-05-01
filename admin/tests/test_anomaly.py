"""Robust MAD anomaly module — pure-function tests.

The detector lives in :mod:`app.anomaly` and is consumed by bowl /
litter monitors. We test the math directly here (no monitor / sightings
plumbing) so a regression shows up as a concrete failed assertion rather
than as a flaky-monitor symptom three layers up.
"""
from __future__ import annotations

from app import anomaly


def test_robust_score_returns_zero_on_empty_baseline():
    assert anomaly.robust_score(5, []) == 0.0


def test_robust_score_zero_when_today_equals_median():
    # Steady 5/day baseline → today=5 → no anomaly, score == 0.
    score = anomaly.robust_score(5, [5, 5, 5, 5, 5, 5])
    assert abs(score) < 0.5  # floor on MAD bumps this slightly above 0


def test_robust_score_positive_when_today_below_median():
    # Today is way below baseline — score should be solidly positive
    # (sign convention: + means "less than usual / drop").
    score = anomaly.robust_score(0, [10, 10, 11, 9, 10, 10, 10])
    assert score > 3.5, f"expected anomaly, got {score}"


def test_robust_score_negative_when_today_above_median():
    score = anomaly.robust_score(20, [5, 5, 6, 5, 5, 4, 5])
    assert score < -3.5, f"expected high-side anomaly, got {score}"


def test_robust_score_resists_single_outlier():
    """Mean-based detection would say "5 is normal" with a [50] day in the
    baseline (mean = 11). Median+MAD rejects the outlier."""
    score = anomaly.robust_score(5, [5, 5, 5, 5, 5, 50])
    # 5 is the median, today=5 → close to zero score
    assert abs(score) < 1.0


def test_is_anomaly_below_min_baseline_returns_false():
    """Brand-new install with two days of data shouldn't false-fire."""
    assert anomaly.is_anomaly(0, [10, 10], min_baseline=3) is False


def test_snapshot_populates_explanation():
    snap = anomaly.snapshot(2, [8, 8, 9, 7, 8, 8, 8])
    assert snap.is_anomaly is True
    assert snap.median == 8
    msg = anomaly.anomaly_explanation(snap, units="visits")
    # Plain-language, no jargon. Tells the owner concrete numbers.
    assert "Today" in msg
    assert "Lower than usual" in msg
    assert "z-score" not in msg.lower()  # never leak the math


def test_snapshot_no_anomaly_explanation_is_quiet():
    snap = anomaly.snapshot(8, [8, 8, 9, 7, 8, 8, 8])
    msg = anomaly.anomaly_explanation(snap, units="visits")
    assert "Lower than usual" not in msg
    assert "Higher than usual" not in msg


def test_snapshot_zero_baseline_is_safe():
    """Empty baseline must not raise — explanation falls through to a
    "no history yet" string."""
    snap = anomaly.snapshot(3, [])
    assert snap.is_anomaly is False
    msg = anomaly.anomaly_explanation(snap)
    assert "Not enough history" in msg


def test_robust_score_handles_all_zero_baseline():
    """Pet recently introduced to a bowl — every day is 0. With MAD floor
    we don't fire on today=0 but we also don't blow up on today=1."""
    score = anomaly.robust_score(0, [0, 0, 0, 0, 0, 0])
    assert abs(score) < 1.0
    # today bumps to 1 — that's *above* median 0, so score is negative
    score2 = anomaly.robust_score(1, [0, 0, 0, 0, 0, 0])
    assert score2 < 0


# ---- conformal anomaly score (v3) -------------------------------------

def test_conformal_returns_none_below_min_history():
    """Brand-new pets shouldn't get a conformal verdict until enough
    history accrues — we want to fall back to MAD until then."""
    short = [5, 6, 4, 5, 5, 6, 5]   # only 7 days, < CONFORMAL_MIN_HISTORY
    assert anomaly.conformal_p_value(2, short) is None


def test_conformal_p_low_for_extreme_today():
    """Today is very far from the median → tiny p-value (top of rank)."""
    baseline = [5, 6, 4, 5, 5, 6, 7, 5, 4, 6, 5, 5, 6, 4, 5]
    p = anomaly.conformal_p_value(0, baseline)
    assert p is not None
    assert p < 0.10, f"expected top 10%, got p={p}"


def test_conformal_p_high_for_typical_today():
    """Today sits at the median → large p-value (totally normal)."""
    baseline = [5, 6, 4, 5, 5, 6, 7, 5, 4, 6, 5, 5, 6, 4, 5]
    p = anomaly.conformal_p_value(5, baseline)
    assert p is not None
    assert p >= 0.5, f"expected boring day, got p={p}"


def test_conformal_explanation_quiet_for_normal_days():
    """Owners shouldn't see a chip for days inside the unusual band."""
    assert anomaly.conformal_explanation(0.5) == ""
    # Slightly unusual is still quiet — chip kicks in <= 20%.
    assert anomaly.conformal_explanation(0.30) == ""


def test_conformal_explanation_top_one_pct():
    msg = anomaly.conformal_explanation(0.01)
    assert "top 1%" in msg


def test_conformal_explanation_none_returns_empty():
    """Caller passes None when not enough history — string is empty."""
    assert anomaly.conformal_explanation(None) == ""
