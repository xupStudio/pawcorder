"""Tests for the /pets/health overview aggregator + system uptime ribbon.

We seed the sightings + reliability NDJSONs, build the overview, and
assert chart fragments + score signals come out correctly.
"""
from __future__ import annotations

import importlib
import json
import time

import pytest
import yaml


def _has_pro() -> bool:
    """True when the Pro pet_health module is installed; OSS skips
    Pro-driven assertions like absence_anomaly."""
    try:
        importlib.import_module("app.pro.pet_health")
        return True
    except ModuleNotFoundError:
        return False


pro_only = pytest.mark.skipif(not _has_pro(), reason="Pro pet_health not installed")


def _seed_pet(pet_id: str, name: str | None = None) -> None:
    from app import pets_store
    pets_store.PETS_YAML.parent.mkdir(parents=True, exist_ok=True)
    pets_store.PETS_YAML.write_text(yaml.safe_dump({"pets": [{
        "pet_id": pet_id, "name": name or pet_id.title(),
        "species": "cat", "notes": "",
        "match_threshold": 0.0, "photos": [],
    }]}, sort_keys=False))


def _seed_sightings(rows: list[dict]) -> None:
    from app import recognition
    recognition.SIGHTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with recognition.SIGHTINGS_LOG.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _row(*, ts: float, camera: str = "kitchen",
          pet_id: str = "mochi") -> dict:
    return {
        "event_id": f"evt-{ts}", "camera": camera, "label": "cat",
        "pet_id": pet_id, "pet_name": pet_id.title(),
        "score": 0.9, "confidence": "high",
        "start_time": ts, "end_time": ts + 2,
    }


# ---- overview_for_all_pets --------------------------------------------

def test_overview_returns_empty_when_no_pets(data_dir):
    from app import pet_health_overview
    assert pet_health_overview.overview_for_all_pets() == []


def test_overview_renders_activity_chart_when_history_exists(data_dir):
    _seed_pet("mochi")
    now = time.time()
    rows = [_row(ts=now - i * 3600) for i in range(20)]
    _seed_sightings(rows)
    from app import pet_health_overview
    result = pet_health_overview.overview_for_all_pets(now=now)
    assert len(result) == 1
    ov = result[0]
    assert ov.pet_id == "mochi"
    # Stacked bars chart should be non-empty when there are sightings.
    assert "<svg" in ov.activity_chart_svg
    assert "rect" in ov.activity_chart_svg


@pro_only
def test_overview_score_drops_on_absence(data_dir, monkeypatch):
    """No sightings at all → absence anomaly → score dial in red band."""
    _seed_pet("mochi")
    _seed_sightings([])
    from app import pet_health_overview
    result = pet_health_overview.overview_for_all_pets()
    assert len(result) == 1
    ov = result[0]
    assert ov.absence_anomaly is True
    # Score drops at least the absence penalty (25). Other detectors
    # may add more, so we use <= rather than ==.
    assert ov.score <= 75
    # Reasons list contains the absence reason key.
    assert "not_seen" in ov.score_reasons


def test_timeline_includes_zero_days(data_dir):
    """A day with no sightings should still appear in the timeline so
    the chart shows the gap."""
    _seed_pet("mochi")
    now = time.time()
    rows = [_row(ts=now - 3600), _row(ts=now - 5 * 86400)]
    _seed_sightings(rows)
    from app import pet_health_overview
    result = pet_health_overview.overview_for_all_pets(now=now)
    days = result[0].timeline_days
    assert len(days) == pet_health_overview.OVERVIEW_DAYS
    # Sum of totals == total sightings rendered into the chart.
    assert sum(d.total for d in days) == 2


def test_heatmap_buckets_by_hour_and_weekday(data_dir):
    _seed_pet("mochi")
    now = time.time()
    # Pile sightings into one specific hour.
    base = int(now) - (int(now) % 86400)   # midnight today
    rows = [_row(ts=base + 14 * 3600 + i)
             for i in range(20)]
    _seed_sightings(rows)
    from app import pet_health_overview
    result = pet_health_overview.overview_for_all_pets(now=now)
    assert "<svg" in result[0].heatmap_svg


# ---- system uptime ribbon ----------------------------------------------

def test_uptime_ribbon_returns_svg_even_without_ledger(data_dir):
    from app import pet_health_overview, svg_charts
    svg = pet_health_overview.system_uptime_ribbon(days=7)
    # Missing-ledger case → all-grey "no data" ribbon. Verify SVG
    # parses, contains 7 rects, all painted with the muted colour.
    import xml.etree.ElementTree as ET
    root = ET.fromstring(svg)
    rects = root.findall(".//rect")
    assert len(rects) == 7
    assert all(r.get("fill") == svg_charts.COLOR_MUTED for r in rects)


def test_uptime_ribbon_marks_failed_day_red(data_dir):
    """Seed the reliability ledger with a failure-heavy day; the
    ribbon should render that block as the bad colour."""
    from app import reliability, svg_charts, pet_health_overview
    now = time.time()
    yesterday = now - 86400
    # 50 ok + 50 fail = 50% — well below the 99% threshold.
    rel_path = reliability.LEDGER_PATH
    rel_path.parent.mkdir(parents=True, exist_ok=True)
    with rel_path.open("w", encoding="utf-8") as f:
        for _ in range(50):
            f.write(json.dumps({
                "ts": yesterday, "subsystem": "camera",
                "name": "cam1", "outcome": "ok", "message": "",
            }) + "\n")
        for _ in range(50):
            f.write(json.dumps({
                "ts": yesterday, "subsystem": "camera",
                "name": "cam1", "outcome": "fail", "message": "boom",
            }) + "\n")
    svg = pet_health_overview.system_uptime_ribbon(days=7)
    # COLOR_BAD should appear in the SVG.
    assert svg_charts.COLOR_BAD in svg
