"""Tests for the inline-SVG chart helpers.

We don't validate visual output (an actual rendered SVG comparison
needs a headless browser). Instead we check structural invariants:
the function returns valid-looking SVG, contains the expected
element types, and handles edge cases without crashing.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET


def _parse(svg: str):
    """SVG should be well-formed XML — parsing it is a cheap
    "did we close every tag" smoke test."""
    return ET.fromstring(svg)


def test_sparkline_renders_polyline():
    from app import svg_charts
    svg = svg_charts.sparkline([1, 2, 3, 4, 5])
    root = _parse(svg)
    # Single polyline + final dot.
    polylines = root.findall(".//polyline")
    assert len(polylines) == 1


def test_sparkline_handles_empty_input():
    from app import svg_charts
    svg = svg_charts.sparkline([])
    # No polyline — just the placeholder svg root.
    assert "<polyline" not in svg
    _parse(svg)  # still well-formed


def test_sparkline_single_value_centers_dot():
    from app import svg_charts
    svg = svg_charts.sparkline([3.0], width=100)
    # Center coordinate should be 50 (width/2) — checked by string match
    # since computing it from the SVG text would re-implement the helper.
    assert "50.0" in svg or 'cx="50.' in svg


def test_bar_chart_threshold_line_renders_above_max():
    from app import svg_charts
    # Bars peak at 3 but threshold is 12 — the threshold line still
    # has to fit inside the chart, i.e. ymax expands to threshold.
    svg = svg_charts.bar_chart(
        ["a", "b", "c"], [1, 2, 3], threshold=12, threshold_label="cap",
    )
    assert "stroke-dasharray" in svg
    assert "12" in svg


def test_bar_chart_handles_empty_values():
    from app import svg_charts
    svg = svg_charts.bar_chart([], [])
    _parse(svg)


def test_uptime_ribbon_colors_blocks():
    from app import svg_charts
    svg = svg_charts.uptime_ribbon([True, False, True])
    # Three rects, two colors — green for OK, red for down.
    root = _parse(svg)
    rects = root.findall(".//rect")
    assert len(rects) == 3
    fills = [r.get("fill") for r in rects]
    assert fills.count(svg_charts.COLOR_OK) == 2
    assert fills.count(svg_charts.COLOR_BAD) == 1


def test_uptime_ribbon_tri_state():
    """None blocks render in the muted (no-data) colour, distinct from
    both ok and down. Catches a deploy that silently breaks the
    reliability writer painting full-green."""
    from app import svg_charts
    svg = svg_charts.uptime_ribbon([True, None, False])
    root = _parse(svg)
    fills = [r.get("fill") for r in root.findall(".//rect")]
    assert {svg_charts.COLOR_OK, svg_charts.COLOR_MUTED, svg_charts.COLOR_BAD} == set(fills)


def test_health_score_dial_colour_band():
    from app import svg_charts
    # Score < 60 should pick the bad colour.
    bad_svg = svg_charts.health_score_dial(40)
    assert svg_charts.COLOR_BAD in bad_svg
    # 80+ should pick OK.
    ok_svg = svg_charts.health_score_dial(95)
    assert svg_charts.COLOR_OK in ok_svg
    # Mid → warn.
    mid_svg = svg_charts.health_score_dial(70)
    assert svg_charts.COLOR_WARN in mid_svg


def test_stacked_bars_handles_missing_values():
    """If a series is shorter than labels, the missing slots count as
    zero — chart still renders without crashing."""
    from app import svg_charts
    svg = svg_charts.stacked_bars(
        ["Mon", "Tue", "Wed"],
        [("A", [1.0, 2.0], "#000"),    # only 2 of 3 values
         ("B", [0.5, 0.5, 0.5], "#fff")],
    )
    _parse(svg)
    # Legend should list both series.
    assert "<text" in svg


def test_heatmap_grid_handles_zeros():
    from app import svg_charts
    svg = svg_charts.heatmap_grid([[0, 0], [0, 0]])
    _parse(svg)


def test_heatmap_grid_intensity_scales():
    """The brightest cell should have higher fill-opacity than the
    dimmest — if not, a busy hour and a quiet hour would look the same."""
    from app import svg_charts
    svg = svg_charts.heatmap_grid([[1, 100], [10, 50]])
    # We can't easily map specific cells back without re-implementing;
    # just assert that more than one fill-opacity value appears.
    import re
    opacities = set(re.findall(r'fill-opacity="([^"]+)"', svg))
    assert len(opacities) > 1
