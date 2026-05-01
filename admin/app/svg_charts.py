"""Inline-SVG chart helpers for the admin UI and the vet pack.

Why hand-rolled SVG and not Chart.js / ApexCharts:

  * **Printable.** The vet pack is HTML the owner prints / saves to PDF
    at the vet's office. JS-rendered charts die on print: print engines
    snapshot the page mid-paint, and the canvas isn't always ready.
    Inline SVG renders the same in print as on screen, every time.
  * **No CDN dependency.** Pawcorder's pitch is "nothing leaves your
    house". Pulling Chart.js from a CDN to draw a sparkline contradicts
    that. Bundling the JS adds ~80 KB per page load for charts users
    might never view.
  * **Server-rendered.** All the data already lives in the route
    handler — turning it into SVG strings there is a few dozen lines
    and zero JS. Hovers / tooltips that *do* need interactivity get a
    thin Alpine.js wrapper at the call site.

Public helpers — all return SVG strings ready to drop into a Jinja
template via ``{{ chart | safe }}``:

  * :func:`sparkline` — single-series compact line chart.
  * :func:`bar_chart` — labelled bars with optional threshold line.
  * :func:`uptime_ribbon` — N-block green/red ribbon for SLO tiles.
  * :func:`stacked_bars` — multi-camera daily stack (vet pack timeline).
  * :func:`heatmap_grid` — 24×7 cells (hour × weekday activity heatmap).

All sizes are in pixels but the SVG ``viewBox`` is set so the chart
scales with its container — drop into a 100% width div and it fills.
"""
from __future__ import annotations

import html
import math
from typing import Iterable, Optional, Sequence


# Brand palette — single source of truth so charts match the rest of
# the admin (Tailwind brand-* classes use these exact hex values).
COLOR_BRAND = "#f37416"          # brand-500
COLOR_BRAND_LIGHT = "#fbd7a8"    # brand-200
COLOR_OK = "#16a34a"             # green-600
COLOR_WARN = "#d97706"           # amber-600
COLOR_BAD = "#dc2626"            # red-600
COLOR_GRID = "#e2e8f0"           # slate-200
COLOR_TEXT = "#475569"           # slate-600
COLOR_MUTED = "#94a3b8"          # slate-400


def _esc(s: object) -> str:
    """SVG text needs HTML escaping (it's XML-ish). Cheap wrapper."""
    return html.escape(str(s))


def _fmt(value: float) -> str:
    """Compact number rendering for axis labels — `12.0` ⇒ `12`,
    `0.5` stays `0.5`. Avoids cluttering with trailing zeros."""
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def sparkline(
    values: Sequence[float],
    *,
    width: int = 120,
    height: int = 32,
    stroke: str = COLOR_BRAND,
    fill: Optional[str] = None,
    show_dot: bool = True,
    label_last: bool = False,
) -> str:
    """Single-series mini chart — fits inline next to a number.

    ``label_last`` draws the last value as text at the right edge so
    the sparkline can stand on its own (used in the dashboard tile
    where there's no number beside it).
    """
    if not values:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true"></svg>'

    pad_x, pad_y = 2, 4
    inner_w = max(1, width - pad_x * 2 - (24 if label_last else 0))
    inner_h = max(1, height - pad_y * 2)

    vmin = min(values)
    vmax = max(values)
    span = max(vmax - vmin, 1e-9)

    n = len(values)
    if n == 1:
        # Center a single value rather than letting it cling to x=0.
        xs = [pad_x + inner_w / 2]
    else:
        xs = [pad_x + i * inner_w / (n - 1) for i in range(n)]
    ys = [pad_y + inner_h - (v - vmin) / span * inner_h for v in values]

    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="trend chart">'
    ]
    if fill:
        # Closed polygon for an "area under the line" feel.
        area_pts = pts + f" {xs[-1]:.1f},{height - pad_y} {xs[0]:.1f},{height - pad_y}"
        parts.append(f'<polygon points="{area_pts}" fill="{fill}" />')
    parts.append(
        f'<polyline points="{pts}" fill="none" stroke="{stroke}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />'
    )
    if show_dot:
        parts.append(
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2" '
            f'fill="{stroke}" />'
        )
    if label_last:
        parts.append(
            f'<text x="{width - 2}" y="{height / 2 + 4}" '
            f'font-size="11" text-anchor="end" fill="{COLOR_TEXT}" '
            f'font-family="system-ui">{_esc(_fmt(values[-1]))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def bar_chart(
    labels: Sequence[str],
    values: Sequence[float],
    *,
    width: int = 640,
    height: int = 200,
    bar_color: str = COLOR_BRAND,
    threshold: Optional[float] = None,
    threshold_label: str = "",
    y_axis_label: str = "",
    max_label_every: int = 1,
) -> str:
    """Vertical bar chart with optional horizontal threshold line.

    ``max_label_every`` thins the x-axis labels — for a 30-day timeline
    pass 5 to get every 5th day. Saves clutter on narrow screens.
    """
    if not values:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true"></svg>'

    if len(labels) != len(values):
        labels = list(labels) + [""] * (len(values) - len(labels))

    pad_l, pad_r, pad_t, pad_b = 32, 12, 14, 28
    inner_w = max(1, width - pad_l - pad_r)
    inner_h = max(1, height - pad_t - pad_b)

    # If threshold's higher than the max bar (e.g. cat usually goes 4×
    # but threshold is 12), the line still needs to render — set ymax
    # to whichever is bigger.
    raw_max = max(max(values), threshold or 0)
    ymax = raw_max if raw_max > 0 else 1.0

    n = len(values)
    bar_total_w = inner_w / n
    bar_w = max(2.0, bar_total_w * 0.7)

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="bar chart" font-family="system-ui">'
    ]
    # Axis baselines.
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + inner_h}" x2="{pad_l + inner_w}" '
        f'y2="{pad_t + inner_h}" stroke="{COLOR_GRID}" />'
    )

    # Y-axis ticks (3 levels: 0, mid, max).
    for frac, label_value in ((0.0, 0.0), (0.5, ymax / 2), (1.0, ymax)):
        y = pad_t + inner_h - frac * inner_h
        parts.append(
            f'<line x1="{pad_l - 3}" y1="{y:.1f}" x2="{pad_l + inner_w}" '
            f'y2="{y:.1f}" stroke="{COLOR_GRID}" stroke-dasharray="2,3" />'
        )
        parts.append(
            f'<text x="{pad_l - 5}" y="{y + 3:.1f}" font-size="10" '
            f'text-anchor="end" fill="{COLOR_TEXT}">{_esc(_fmt(label_value))}</text>'
        )

    # Bars.
    for i, (lbl, v) in enumerate(zip(labels, values)):
        h_px = (v / ymax) * inner_h if ymax > 0 else 0
        x = pad_l + i * bar_total_w + (bar_total_w - bar_w) / 2
        y = pad_t + inner_h - h_px
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{h_px:.1f}" fill="{bar_color}" rx="1.5">'
            f'<title>{_esc(lbl)}: {_esc(_fmt(v))}</title></rect>'
        )
        if i % max_label_every == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" '
                f'font-size="9.5" text-anchor="middle" fill="{COLOR_MUTED}">'
                f'{_esc(lbl)}</text>'
            )

    if threshold is not None and threshold > 0:
        ty = pad_t + inner_h - (threshold / ymax) * inner_h
        parts.append(
            f'<line x1="{pad_l}" y1="{ty:.1f}" x2="{pad_l + inner_w}" '
            f'y2="{ty:.1f}" stroke="{COLOR_BAD}" stroke-width="1" '
            f'stroke-dasharray="4,3" />'
        )
        if threshold_label:
            parts.append(
                f'<text x="{pad_l + inner_w - 4}" y="{ty - 3:.1f}" '
                f'font-size="10" text-anchor="end" fill="{COLOR_BAD}">'
                f'{_esc(threshold_label)}: {_esc(_fmt(threshold))}</text>'
            )

    if y_axis_label:
        parts.append(
            f'<text x="6" y="{pad_t + inner_h / 2:.1f}" font-size="10" '
            f'fill="{COLOR_TEXT}" transform="rotate(-90 6 {pad_t + inner_h / 2:.1f})" '
            f'text-anchor="middle">{_esc(y_axis_label)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def uptime_ribbon(
    blocks: Sequence,
    *,
    width: int = 240,
    height: int = 18,
    title_each: Optional[Sequence[str]] = None,
) -> str:
    """Horizontal ribbon of coloured rectangles — one per block.

    Each block is a tri-state value:
      * ``True``  → green (the day was healthy)
      * ``False`` → red   (the day had failures)
      * ``None``  → grey  (no data — important so a deploy that
        silently breaks the reliability writer doesn't paint full
        green and lull the owner into trusting it)
    Booleans-only callers still work; pass ``None`` for "no data" days.
    """
    if not blocks:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true"></svg>'

    n = len(blocks)
    gap = 1.5
    block_w = max(1.0, (width - gap * (n - 1)) / n)

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="uptime ribbon">'
    ]
    for i, val in enumerate(blocks):
        x = i * (block_w + gap)
        if val is None:
            color = COLOR_MUTED
            label = "no data"
        elif val:
            color = COLOR_OK
            label = "ok"
        else:
            color = COLOR_BAD
            label = "down"
        title = (title_each[i] if title_each and i < len(title_each) else label)
        parts.append(
            f'<rect x="{x:.1f}" y="0" width="{block_w:.1f}" '
            f'height="{height}" rx="2" fill="{color}">'
            f'<title>{_esc(title)}</title></rect>'
        )
    parts.append("</svg>")
    return "".join(parts)


def stacked_bars(
    labels: Sequence[str],
    series: Sequence[tuple[str, Sequence[float], str]],
    *,
    width: int = 640,
    height: int = 220,
    max_label_every: int = 1,
) -> str:
    """Vertical stacked bars — one column per label, one stack per series.

    ``series`` is ``[(series_name, values, color), ...]``. Used by the
    vet pack's "30 days, broken down by camera" chart so the vet sees
    not just total activity but where the cat actually was.
    """
    if not labels or not series:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true"></svg>'

    n = len(labels)
    # Each column's total = sum across series at that index.
    totals = [
        sum((s[1][i] if i < len(s[1]) else 0) for s in series)
        for i in range(n)
    ]
    ymax = max(totals) if any(t > 0 for t in totals) else 1.0

    pad_l, pad_r, pad_t, pad_b = 36, 100, 14, 28  # extra right pad for legend
    inner_w = max(1, width - pad_l - pad_r)
    inner_h = max(1, height - pad_t - pad_b)
    bar_total_w = inner_w / n
    bar_w = max(2.0, bar_total_w * 0.7)

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="stacked bar chart" font-family="system-ui">'
    ]
    # Axis + grid.
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + inner_h}" x2="{pad_l + inner_w}" '
        f'y2="{pad_t + inner_h}" stroke="{COLOR_GRID}" />'
    )
    for frac in (0.0, 0.5, 1.0):
        y = pad_t + inner_h - frac * inner_h
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + inner_w}" '
            f'y2="{y:.1f}" stroke="{COLOR_GRID}" stroke-dasharray="2,3" />'
        )
        parts.append(
            f'<text x="{pad_l - 5}" y="{y + 3:.1f}" font-size="10" '
            f'text-anchor="end" fill="{COLOR_TEXT}">{_esc(_fmt(ymax * frac))}</text>'
        )

    # Bars — stack from baseline upward.
    for i, lbl in enumerate(labels):
        x = pad_l + i * bar_total_w + (bar_total_w - bar_w) / 2
        running = 0.0
        for s_name, s_values, s_color in series:
            v = s_values[i] if i < len(s_values) else 0
            if v <= 0:
                continue
            seg_h = (v / ymax) * inner_h
            y = pad_t + inner_h - (running + v) / ymax * inner_h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                f'height="{seg_h:.1f}" fill="{s_color}">'
                f'<title>{_esc(s_name)} on {_esc(lbl)}: {_esc(_fmt(v))}</title>'
                f'</rect>'
            )
            running += v
        if i % max_label_every == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" '
                f'font-size="9.5" text-anchor="middle" fill="{COLOR_MUTED}">'
                f'{_esc(lbl)}</text>'
            )

    # Legend on the right.
    legend_x = pad_l + inner_w + 10
    for j, (s_name, _values, s_color) in enumerate(series):
        ly = pad_t + j * 16
        parts.append(
            f'<rect x="{legend_x}" y="{ly}" width="10" height="10" '
            f'fill="{s_color}" rx="1" />'
        )
        parts.append(
            f'<text x="{legend_x + 14}" y="{ly + 9}" font-size="11" '
            f'fill="{COLOR_TEXT}">{_esc(s_name)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def heatmap_grid(
    counts: Sequence[Sequence[float]],
    *,
    width: int = 640,
    height: int = 180,
    row_labels: Optional[Sequence[str]] = None,
    col_labels: Optional[Sequence[str]] = None,
) -> str:
    """Generic 2-D heatmap — rows × cols of cells coloured by intensity.

    Used for the hour-of-day × weekday activity grid on the health
    overview page so owners can see "Mochi is most active Tuesday
    evenings" without reading numbers.
    """
    if not counts or not counts[0]:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true"></svg>'

    rows = len(counts)
    cols = len(counts[0])
    pad_l, pad_r, pad_t, pad_b = 36, 12, 14, 22
    inner_w = max(1, width - pad_l - pad_r)
    inner_h = max(1, height - pad_t - pad_b)
    cell_w = inner_w / cols
    cell_h = inner_h / rows

    flat_max = max((v for row in counts for v in row), default=0)
    if flat_max <= 0:
        flat_max = 1.0

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="activity heatmap" font-family="system-ui">'
    ]
    for r in range(rows):
        for c in range(cols):
            v = counts[r][c]
            intensity = max(0.0, min(1.0, v / flat_max))
            # Scale alpha so an empty cell is light grey, full cell is brand.
            a = 0.08 + intensity * 0.85
            x = pad_l + c * cell_w
            y = pad_t + r * cell_h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w - 0.5:.1f}" '
                f'height="{cell_h - 0.5:.1f}" '
                f'fill="{COLOR_BRAND}" fill-opacity="{a:.3f}">'
                f'<title>{_esc((row_labels[r] if row_labels else r))} '
                f'/ {_esc((col_labels[c] if col_labels else c))}: '
                f'{_esc(_fmt(v))}</title></rect>'
            )

    if row_labels:
        for r, lbl in enumerate(row_labels[:rows]):
            y = pad_t + r * cell_h + cell_h / 2 + 3
            parts.append(
                f'<text x="{pad_l - 4}" y="{y:.1f}" font-size="9.5" '
                f'text-anchor="end" fill="{COLOR_TEXT}">{_esc(lbl)}</text>'
            )
    if col_labels:
        for c, lbl in enumerate(col_labels[:cols]):
            x = pad_l + c * cell_w + cell_w / 2
            parts.append(
                f'<text x="{x:.1f}" y="{height - 6}" font-size="9.5" '
                f'text-anchor="middle" fill="{COLOR_MUTED}">{_esc(lbl)}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def health_score_dial(
    score: float,
    *,
    size: int = 80,
    label: str = "",
) -> str:
    """Radial gauge 0..100 with colour bands. Compact tile widget.

    Score colour: ≥80 green, ≥60 amber, < 60 red. Owners read the
    colour at a glance; the number is for the curious.
    """
    score = max(0.0, min(100.0, float(score)))
    if score >= 80:
        color = COLOR_OK
    elif score >= 60:
        color = COLOR_WARN
    else:
        color = COLOR_BAD

    cx = cy = size / 2
    r = size / 2 - 6
    circumference = 2 * math.pi * r
    # Start at 12 o'clock (rotate -90deg) and fill clockwise.
    dash = circumference * (score / 100)
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'role="img" aria-label="health score" font-family="system-ui">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
        f'stroke="{COLOR_GRID}" stroke-width="6" />'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
        f'stroke="{color}" stroke-width="6" stroke-linecap="round" '
        f'stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" '
        f'transform="rotate(-90 {cx} {cy})" />'
        f'<text x="{cx}" y="{cy + 4}" font-size="18" font-weight="600" '
        f'text-anchor="middle" fill="{color}">{int(score)}</text>'
        + (
            f'<text x="{cx}" y="{cy + 18}" font-size="9" '
            f'text-anchor="middle" fill="{COLOR_MUTED}">{_esc(label)}</text>'
            if label else ""
        )
        + "</svg>"
    )


def dot_strip(
    values: Sequence[float],
    *,
    width: int = 200,
    height: int = 24,
    color: str = COLOR_BRAND,
) -> str:
    """A row of dots, sized by value. Cheaper than a bar chart for very
    short series like "last 7 days at a glance"."""
    if not values:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true"></svg>'

    n = len(values)
    vmax = max(values) if max(values) > 0 else 1.0
    cy = height / 2
    step = width / n
    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="dot strip">'
    ]
    for i, v in enumerate(values):
        cx = step * i + step / 2
        # Radius scales 2..(height/2-2) so even a 0 shows a dot.
        r = 2 + (v / vmax) * (height / 2 - 4)
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
            f'fill="{color}" fill-opacity="0.85"><title>{_esc(_fmt(v))}</title>'
            f'</circle>'
        )
    parts.append("</svg>")
    return "".join(parts)
