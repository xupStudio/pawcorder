"""30-day pet health export for the vet visit.

The use-case: the user goes to the vet, vet asks "how's the activity
been?". Owner pulls up pawcorder, taps a button, gets a single-page
printable summary covering the last 30 days that they hand to the vet.

Output is HTML rather than PDF for two reasons:

  * **CJK fonts.** Owners in Taiwan / Japan / Korea need the report
    in their local script. A stdlib-only PDF generator can't ship a
    CJK font (10+ MB), and pulling reportlab + cjk packages bloats
    the admin container by ~50 MB. HTML with the user's installed
    fonts handles this for free.
  * **Editing.** Vets frequently want to copy a number out, add a
    note, send to a colleague. PDF is a dead-end for that — HTML
    with print-CSS prints to PDF on Cmd-P / Ctrl-P, and is also
    selectable / annotateable in the source format.

The `@media print` block hides nav and forces single-column so the
output is two-page-max even with 30 days of data.

Deliberately decoupled from the LLM diary path — vets don't want
narrative prose, they want raw activity tables.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import time
from dataclasses import dataclass, field
from typing import Optional

from . import config_store, recognition, svg_charts
from .pets_store import Pet, PetStore

# Pro detectors are optional — vet pack still renders without them.
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

# Length of the report window. 30 days matches the standard vet
# history form — long enough to spot a behavioural shift, short
# enough that the printout fits on two A4 pages.
REPORT_DAYS = 30


@dataclass
class DailyRow:
    """One row in the activity table — per-day totals."""
    date: str
    sightings: int
    cameras: list[str]
    first_seen_hour: Optional[int] = None
    last_seen_hour: Optional[int] = None


@dataclass
class VetPack:
    """Aggregated stats handed to the template. Pure data — no rendering."""
    pet_id: str
    pet_name: str
    species: str
    notes: str
    generated_at: float
    period_start: float                 # unix seconds, inclusive
    period_end: float                   # unix seconds, inclusive
    total_sightings: int
    days_seen: int                      # how many of the 30 days had ≥1 sighting
    days_absent: int                    # full days with zero sightings
    avg_per_day: float
    cameras: dict[str, int]             # camera_name → total sightings
    longest_absence_hours: float        # longest gap in the period
    rows: list[DailyRow]                # newest day first
    # Pro signals — empty / zero when the modules aren't installed or
    # the owner hasn't enabled the matching feature. Vet pack still
    # renders cleanly when these are absent.
    litter_daily: list[int] = field(default_factory=list)        # 14 days, oldest first
    water_daily: list[int] = field(default_factory=list)
    food_daily: list[int] = field(default_factory=list)
    fight_events: list[dict] = field(default_factory=list)       # last 30 days
    posture_flags: list[dict] = field(default_factory=list)      # vomit / gait flags


def build_vet_pack(pet: Pet, *, now: Optional[float] = None,
                    sightings: Optional[list[dict]] = None) -> VetPack:
    """Aggregate sightings into per-day stats over the last REPORT_DAYS.

    Caller can pass a `sightings` list to skip the file read (tests
    and the route handler that already loaded a wider window).
    """
    now = now or time.time()
    period_end = now
    # Anchor the window on the start of the local day REPORT_DAYS-1
    # back so the table's day buckets exactly tile the window. Without
    # this alignment the daily loop emits 30 day_keys covering today
    # plus 29 days back (= 30 calendar days), but `period_start = now -
    # 30*86400` reaches one day earlier — sightings in that overlap
    # zone would be filtered IN but never bucketed, under-counting the
    # total. Anchoring to local midnight makes the two boundaries match.
    midnight_today = time.mktime(time.strptime(
        time.strftime("%Y-%m-%d", time.localtime(now)), "%Y-%m-%d"
    ))
    period_start = midnight_today - (REPORT_DAYS - 1) * 86400
    if sightings is None:
        sightings = recognition.read_sightings(
            limit=50_000, since=period_start,
        )
    rows_for_pet = [
        r for r in sightings
        if r.get("pet_id") == pet.pet_id
        and float(r.get("start_time") or 0) >= period_start
    ]

    # Bucket by local-day so the columns line up with what the user
    # sees on the dashboard (which also uses time.localtime).
    by_day: dict[str, list[dict]] = {}
    for r in rows_for_pet:
        ts = float(r.get("start_time") or 0)
        if ts <= 0:
            continue
        day_key = time.strftime("%Y-%m-%d", time.localtime(ts))
        by_day.setdefault(day_key, []).append(r)

    # Build a row for every day in the window — including days with
    # zero sightings. Vets want to see the absence days, not a sparse
    # table that hides them.
    rows: list[DailyRow] = []
    cameras_total: dict[str, int] = {}
    for i in range(REPORT_DAYS):
        ts = now - i * 86400
        day_key = time.strftime("%Y-%m-%d", time.localtime(ts))
        day_rows = by_day.get(day_key, [])
        cams: dict[str, int] = {}
        first_h: Optional[int] = None
        last_h: Optional[int] = None
        for r in day_rows:
            cam = str(r.get("camera") or "")
            if cam:
                cams[cam] = cams.get(cam, 0) + 1
                cameras_total[cam] = cameras_total.get(cam, 0) + 1
            t = float(r.get("start_time") or 0)
            if t > 0:
                h = time.localtime(t).tm_hour
                if first_h is None or h < first_h:
                    first_h = h
                if last_h is None or h > last_h:
                    last_h = h
        rows.append(DailyRow(
            date=day_key,
            sightings=len(day_rows),
            cameras=sorted(cams.keys(), key=lambda k: -cams[k]),
            first_seen_hour=first_h,
            last_seen_hour=last_h,
        ))

    total_sightings = sum(r.sightings for r in rows)
    days_seen = sum(1 for r in rows if r.sightings > 0)
    days_absent = REPORT_DAYS - days_seen
    avg_per_day = total_sightings / REPORT_DAYS if REPORT_DAYS else 0.0

    # Longest gap — scan timestamps, find max delta between consecutive
    # sightings (and between the boundary and the first/last sighting).
    longest_gap_h = 0.0
    if rows_for_pet:
        ts_sorted = sorted(
            float(r.get("start_time") or 0) for r in rows_for_pet
            if (r.get("start_time") or 0) > 0
        )
        if ts_sorted:
            longest_gap_h = (ts_sorted[0] - period_start) / 3600.0
            for a, b in zip(ts_sorted, ts_sorted[1:]):
                gap_h = (b - a) / 3600.0
                if gap_h > longest_gap_h:
                    longest_gap_h = gap_h
            tail = (period_end - ts_sorted[-1]) / 3600.0
            if tail > longest_gap_h:
                longest_gap_h = tail
    else:
        longest_gap_h = REPORT_DAYS * 24.0

    pack = VetPack(
        pet_id=pet.pet_id,
        pet_name=pet.name,
        species=pet.species,
        notes=pet.notes,
        generated_at=now,
        period_start=period_start,
        period_end=period_end,
        total_sightings=total_sightings,
        days_seen=days_seen,
        days_absent=days_absent,
        avg_per_day=avg_per_day,
        cameras=cameras_total,
        longest_absence_hours=longest_gap_h,
        rows=rows,
    )

    # Layer in the Pro signals. The aggregator's already loaded a wide
    # sightings slice — we re-use it where possible to avoid a second
    # disk read. Each block silently no-ops on OSS builds.
    pack.litter_daily = _litter_daily(pet, sightings, now=now)
    pack.water_daily, pack.food_daily = _bowl_daily(pet, sightings, now=now)
    pack.fight_events = _fight_events_for_pet(pet, sightings, now=now)
    pack.posture_flags = _posture_flags_for_pet(pet, sightings, now=now)
    return pack


# ---- Pro signal helpers -----------------------------------------------

def _litter_daily(pet: Pet, sightings: list[dict], *, now: float,
                   days: int = 14) -> list[int]:
    if litter_monitor is None:
        return []
    cfg = config_store.load_config()
    camera = cfg.litter_box_camera
    if not camera:
        return []
    zone = litter_monitor._resolve_litter_zone(camera)
    # Reuse the live alerter's predicate so the vet pack and the
    # /pets/health chart never disagree about a day's count.
    return recognition.daily_buckets(
        sightings, pet_id=pet.pet_id,
        predicate=litter_monitor.litter_visit_predicate(camera, zone),
        now=now, days=days,
    )


def _bowl_daily(pet: Pet, sightings: list[dict], *, now: float,
                 days: int = 14) -> tuple[list[int], list[int]]:
    """Returns (water_daily, food_daily) — empty when no bowl zones."""
    if bowl_monitor is None:
        return [], []
    bowls = bowl_monitor._discover_bowls()
    if not bowls:
        return [], []

    def _count(kind: str) -> list[int]:
        relevant = [b for b in bowls if b.kind == kind]
        if not relevant:
            return []

        def _hits(r: dict) -> bool:
            return any(bowl_monitor._row_in_bowl(r, b) for b in relevant)

        return recognition.daily_buckets(
            sightings, pet_id=pet.pet_id, predicate=_hits,
            now=now, days=days,
        )

    return _count("water_bowl"), _count("food_bowl")


def _fight_events_for_pet(pet: Pet, sightings: list[dict], *,
                           now: float) -> list[dict]:
    """Last 30 days of fight clusters involving this pet — for the
    vet's "any aggression?" question."""
    if fight_detector is None:
        return []
    rows = [r for r in sightings
            if float(r.get("start_time") or 0) >= now - REPORT_DAYS * 86400]
    clusters = fight_detector._scan_clusters(rows)
    out: list[dict] = []
    for c in clusters:
        if c.pet_a_id != pet.pet_id and c.pet_b_id != pet.pet_id:
            continue
        other = c.pet_b_name if c.pet_a_id == pet.pet_id else c.pet_a_name
        out.append({
            "when": time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(c.started_at)),
            "camera": c.camera,
            "with": other,
            "events": c.event_count,
        })
    return out


def _posture_flags_for_pet(pet: Pet, sightings: list[dict], *,
                             now: float) -> list[dict]:
    """Posture stub flags from the last 30 days. Each entry is a quick
    one-liner the vet skims; the printable version doesn't try to
    render a clip — that's separate from the document anyway."""
    if posture_detector is None:
        return []
    # The posture detector operates on a 1h window in the alerter; for
    # the vet pack we want a 30-day rollup — so we slice manually here.
    rows = [r for r in sightings
            if float(r.get("start_time") or 0) >= now - REPORT_DAYS * 86400]
    out: list[dict] = []
    for snap in posture_detector.vomit_snapshots(now=now, rows=rows):
        if snap.pet_id == pet.pet_id and snap.flagged:
            out.append({"kind": "vomit", "detail": snap.detail,
                        "camera": snap.last_seen_camera or ""})
    for snap in posture_detector.gait_snapshots(now=now, rows=rows):
        if snap.pet_id == pet.pet_id and snap.flagged:
            out.append({"kind": "gait", "detail": snap.detail,
                        "camera": snap.last_seen_camera or ""})
    return out


# ---- HTML rendering ---------------------------------------------------


def _fmt_hour(h: Optional[int]) -> str:
    return f"{h:02d}:00" if h is not None else "—"


def _fmt_date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def render_html(pack: VetPack, *, lang: str = "en") -> str:
    """Render the pack as a self-contained HTML page.

    No external CSS or JS so the file is portable: a vet can save
    the page from the browser, email it, and the recipient still
    gets the same layout. We keep CSS inline with `<style>` rather
    than a `<link>`.
    """
    is_zh = lang.startswith("zh")
    L = _LABELS_ZH if is_zh else _LABELS_EN

    # Header summary tiles.
    summary_tiles = "".join(
        f'<div class="tile"><div class="tile-label">{html.escape(label)}</div>'
        f'<div class="tile-value">{html.escape(str(value))}</div></div>'
        for label, value in [
            (L["total_sightings"], pack.total_sightings),
            (L["days_seen"], f"{pack.days_seen} / {REPORT_DAYS}"),
            (L["avg_per_day"], f"{pack.avg_per_day:.1f}"),
            (L["longest_absence"],
             f"{pack.longest_absence_hours:.1f} {L['hours']}"),
        ]
    )

    # Camera breakdown.
    if pack.cameras:
        cam_items = "".join(
            f'<li>{html.escape(name)} — '
            f'{count} {L["sightings_word"]}</li>'
            for name, count in sorted(pack.cameras.items(), key=lambda kv: -kv[1])
        )
        cam_block = f'<ul class="cameras">{cam_items}</ul>'
    else:
        cam_block = f'<p class="empty">{html.escape(L["no_cameras"])}</p>'

    # Daily rows table.
    body_rows: list[str] = []
    for r in pack.rows:
        sighting_cls = "row-zero" if r.sightings == 0 else ""
        cams = ", ".join(html.escape(c) for c in r.cameras[:3]) or "—"
        body_rows.append(
            f'<tr class="{sighting_cls}">'
            f'<td>{html.escape(r.date)}</td>'
            f'<td class="num">{r.sightings}</td>'
            f'<td>{cams}</td>'
            f'<td class="num">{_fmt_hour(r.first_seen_hour)}</td>'
            f'<td class="num">{_fmt_hour(r.last_seen_hour)}</td>'
            f'</tr>'
        )
    table_body = "\n".join(body_rows)

    notes_block = (
        f'<section class="notes"><h2>{html.escape(L["notes"])}</h2>'
        f'<p>{html.escape(pack.notes)}</p></section>'
        if pack.notes else ""
    )

    period_label = (
        f'{_fmt_date(pack.period_start)} → {_fmt_date(pack.period_end)}'
    )
    generated_label = time.strftime("%Y-%m-%d %H:%M",
                                     time.localtime(pack.generated_at))

    # Activity timeline as inline SVG bar chart. Vets glance at a chart
    # 5× faster than at a 30-row table. Hidden when there's literally
    # zero activity (an empty chart looks like a bug).
    timeline_chart = ""
    if pack.total_sightings > 0:
        # Reverse rows because pack.rows is newest-first and the chart
        # reads left → right oldest → newest.
        ordered = list(reversed(pack.rows))
        labels = [r.date[5:] for r in ordered]   # MM-DD
        values = [float(r.sightings) for r in ordered]
        timeline_chart = (
            f'<section><h2>{html.escape(L["chart_heading"])}</h2>'
            + svg_charts.bar_chart(
                labels, values, width=720, height=200,
                max_label_every=5,
                y_axis_label=L["sightings_word"],
            ) + "</section>"
        )

    # Pro signal sections: each renders only when its data is present.
    pro_sections: list[str] = []
    if pack.litter_daily and any(pack.litter_daily):
        pro_sections.append(
            f'<section class="pro-row"><h2>{html.escape(L["litter_heading"])}</h2>'
            + svg_charts.bar_chart(
                [str(i + 1) for i in range(len(pack.litter_daily))],
                [float(v) for v in pack.litter_daily],
                width=720, height=140,
                max_label_every=2,
                bar_color=svg_charts.COLOR_WARN,
            )
            + f'<p class="meta">{html.escape(L["litter_note"])}</p>'
            + "</section>"
        )
    if pack.water_daily and any(pack.water_daily):
        pro_sections.append(
            f'<section class="pro-row"><h2>{html.escape(L["water_heading"])}</h2>'
            + svg_charts.bar_chart(
                [str(i + 1) for i in range(len(pack.water_daily))],
                [float(v) for v in pack.water_daily],
                width=720, height=140,
                max_label_every=2,
                bar_color="#0ea5e9",
            )
            + "</section>"
        )
    if pack.food_daily and any(pack.food_daily):
        pro_sections.append(
            f'<section class="pro-row"><h2>{html.escape(L["food_heading"])}</h2>'
            + svg_charts.bar_chart(
                [str(i + 1) for i in range(len(pack.food_daily))],
                [float(v) for v in pack.food_daily],
                width=720, height=140,
                max_label_every=2,
                bar_color="#10b981",
            )
            + "</section>"
        )
    if pack.fight_events:
        rows_html = "".join(
            f'<tr><td>{html.escape(e["when"])}</td>'
            f'<td>{html.escape(e["with"])}</td>'
            f'<td>{html.escape(e["camera"])}</td>'
            f'<td class="num">{e["events"]}</td></tr>'
            for e in pack.fight_events
        )
        pro_sections.append(
            f'<section><h2>{html.escape(L["fight_heading"])}</h2>'
            + f'<table><thead><tr>'
            + f'<th>{html.escape(L["col_when"])}</th>'
            + f'<th>{html.escape(L["fight_with"])}</th>'
            + f'<th>{html.escape(L["col_cameras"])}</th>'
            + f'<th class="num">{html.escape(L["fight_events"])}</th>'
            + '</tr></thead><tbody>' + rows_html
            + '</tbody></table></section>'
        )
    if pack.posture_flags:
        # Map internal kind tags to friendly prose — vets and owners
        # read this print-out, not engineers. Unknown keys fall back to
        # the raw tag so a future detector type doesn't silently render
        # an empty cell.
        rows_html = "".join(
            f'<tr><td>{html.escape(L["posture_kind_" + f["kind"]] if "posture_kind_" + f["kind"] in L else f["kind"])}</td>'
            f'<td>{html.escape(f["camera"])}</td>'
            f'<td>{html.escape(f["detail"])}</td></tr>'
            for f in pack.posture_flags
        )
        pro_sections.append(
            f'<section><h2>{html.escape(L["posture_heading"])}</h2>'
            + '<table><thead><tr>'
            + f'<th>{html.escape(L["posture_kind"])}</th>'
            + f'<th>{html.escape(L["col_cameras"])}</th>'
            + f'<th>{html.escape(L["posture_detail"])}</th>'
            + '</tr></thead><tbody>' + rows_html
            + '</tbody></table>'
            + f'<p class="meta">{html.escape(L["posture_note"])}</p>'
            + '</section>'
        )
    pro_blocks_html = "\n".join(pro_sections)

    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<title>{html.escape(L["title"].format(name=pack.pet_name))}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
                  "Noto Sans TC", "PingFang TC", sans-serif;
    margin: 0; padding: 24px 32px; color: #1a1a1a; background: #fff;
  }}
  header {{ border-bottom: 2px solid #111; padding-bottom: 12px; margin-bottom: 18px; }}
  h1 {{ margin: 0; font-size: 22px; }}
  .meta {{ color: #555; font-size: 13px; margin-top: 4px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(4, 1fr);
            gap: 10px; margin: 18px 0; }}
  .tile {{ border: 1px solid #ddd; border-radius: 6px; padding: 10px 12px; }}
  .tile-label {{ color: #666; font-size: 11px; text-transform: uppercase;
                 letter-spacing: 0.5px; }}
  .tile-value {{ font-size: 22px; font-weight: 600; margin-top: 2px; }}
  h2 {{ font-size: 15px; margin: 18px 0 8px; }}
  ul.cameras {{ margin: 0; padding-left: 18px; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px;
           margin-top: 6px; }}
  th, td {{ border-bottom: 1px solid #eee; padding: 5px 8px; text-align: left; }}
  th {{ background: #f6f6f6; font-weight: 600; font-size: 11px;
        text-transform: uppercase; letter-spacing: 0.4px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr.row-zero td {{ color: #b00; }}
  .notes {{ margin-top: 16px; padding: 10px 12px; background: #f8f8f8;
            border-radius: 6px; font-size: 13px; }}
  .empty {{ color: #888; font-style: italic; font-size: 13px; }}
  .actions {{ margin-top: 18px; }}
  .actions button {{
    background: #111; color: #fff; border: 0; padding: 8px 14px;
    border-radius: 6px; font-size: 13px; cursor: pointer;
  }}
  footer {{ margin-top: 24px; color: #888; font-size: 11px; text-align: center; }}

  /* Print: hide the action button, lose the page background, give
     each section a clean page-break-inside: avoid. */
  @media print {{
    body {{ padding: 12mm; }}
    .actions {{ display: none; }}
    section, .tiles, table {{ page-break-inside: auto; }}
  }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(L["title"].format(name=pack.pet_name))}</h1>
  <div class="meta">
    {html.escape(pack.species.title())} ·
    {html.escape(L["period"])}: {html.escape(period_label)} ·
    {html.escape(L["generated"])}: {html.escape(generated_label)}
  </div>
</header>

<section>
  <div class="tiles">{summary_tiles}</div>
</section>

<section>
  <h2>{html.escape(L["cameras_seen"])}</h2>
  {cam_block}
</section>

{timeline_chart}

{pro_blocks_html}

<section>
  <h2>{html.escape(L["daily_breakdown"])}</h2>
  <table>
    <thead>
      <tr>
        <th>{html.escape(L["col_date"])}</th>
        <th class="num">{html.escape(L["col_sightings"])}</th>
        <th>{html.escape(L["col_cameras"])}</th>
        <th class="num">{html.escape(L["col_first"])}</th>
        <th class="num">{html.escape(L["col_last"])}</th>
      </tr>
    </thead>
    <tbody>
      {table_body}
    </tbody>
  </table>
</section>

{notes_block}

<div class="actions">
  <button onclick="window.print()">{html.escape(L["print_button"])}</button>
</div>

<footer>
  {html.escape(L["footer"])}
</footer>
</body>
</html>
"""


# ---- labels (small i18n island, kept here so tests don't pull i18n.py) -

_LABELS_EN = {
    "title": "Vet visit pack — {name}",
    "period": "Period",
    "generated": "Generated",
    "total_sightings": "Total sightings",
    "days_seen": "Days seen",
    "avg_per_day": "Avg / day",
    "longest_absence": "Longest gap",
    "hours": "h",
    "cameras_seen": "Cameras",
    "no_cameras": "No camera activity in this window.",
    "sightings_word": "sightings",
    "chart_heading": "Activity timeline (30 days)",
    "litter_heading": "Litter-box visits (14 days)",
    "litter_note": "Many visits per day, or repeated rapid in-and-out, can be an early sign of a urinary issue.",
    "water_heading": "Water-bowl visits (14 days)",
    "food_heading": "Food-bowl visits (14 days)",
    "fight_heading": "Rough interactions (30 days)",
    "fight_with": "With",
    "fight_events": "Events",
    "posture_heading": "Posture / gait flags (30 days)",
    "posture_kind": "What we saw",
    "posture_kind_vomit": "Looked like it was about to vomit",
    "posture_kind_gait": "Walking looked off",
    "posture_detail": "Where + when",
    "posture_note": "Posture flags are rough hints meant to prompt you to re-watch the clip — not a diagnosis.",
    "col_when": "When",
    "daily_breakdown": "Daily activity (last 30 days)",
    "col_date": "Date",
    "col_sightings": "Sightings",
    "col_cameras": "Cameras",
    "col_first": "First seen",
    "col_last": "Last seen",
    "notes": "Owner notes",
    "print_button": "Print / Save as PDF",
    "footer": "Generated by Pawcorder from camera sightings — not a "
              "substitute for a clinical exam.",
}

# ---- signed share link ------------------------------------------------
#
# Use case: owner is at the vet, doesn't want to hand over their phone.
# They tap "share" → Pawcorder mints a 24h URL the vet opens on their
# own browser. The URL bypasses admin auth (no session) but only works
# until expiry. Lives behind ``/share/vet-pack/...`` which is public
# but read-only and pet-scoped.
#
# We HMAC-sign with ``ADMIN_SESSION_SECRET`` (already in .env, already
# treated as a secret). Rotating that secret invalidates every
# outstanding share link — that's the right safety property.

SHARE_LINK_TTL_SECONDS = 24 * 3600
_SHARE_VERSION = "v1"


def _share_secret() -> bytes:
    """Pull the signing secret from the .env, then derive a per-purpose
    sub-key so the share-link HMAC and the session-cookie HMAC don't
    share material. Domain separation: even if a future code path
    hashes user-controlled input with the raw session secret to produce
    24-byte outputs, an attacker can't replay it as a valid share sig.
    """
    cfg = config_store.load_config()
    if not cfg.admin_session_secret:
        raise RuntimeError("admin_session_secret_required_to_share")
    return hmac.new(
        cfg.admin_session_secret.encode("utf-8"),
        b"vet-pack-share-v1",
        hashlib.sha256,
    ).digest()


def _b64(data: bytes) -> str:
    """URL-safe base64 without padding — keeps query strings clean."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_share_token(pet_id: str, *, ttl: int = SHARE_LINK_TTL_SECONDS,
                      now: Optional[float] = None) -> str:
    """Return ``"<exp>.<sig>"`` — exp is unix-seconds (URL-safe int),
    sig is the HMAC-SHA256 truncated to 24 bytes (192 bits). We pin
    the version into the message to make future rotations possible
    without invalidating the URL format itself."""
    now = now or time.time()
    exp = int(now) + max(60, int(ttl))
    msg = f"{_SHARE_VERSION}|{pet_id}|{exp}".encode("utf-8")
    sig = hmac.new(_share_secret(), msg, hashlib.sha256).digest()[:24]
    return f"{exp}.{_b64(sig)}"


def verify_share_token(pet_id: str, token: str, *,
                        now: Optional[float] = None) -> bool:
    """Constant-time compare. Returns False on any parse / sig / expiry
    failure — caller responds with 410 Gone (link expired or bad)."""
    if not token or "." not in token:
        return False
    exp_str, sig_b64 = token.split(".", 1)
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if (now or time.time()) > exp:
        return False
    msg = f"{_SHARE_VERSION}|{pet_id}|{exp}".encode("utf-8")
    expected = hmac.new(_share_secret(), msg, hashlib.sha256).digest()[:24]
    # ``token`` may be missing padding — restore for the compare. Pad
    # to the next multiple of 4 with '='.
    pad = "=" * ((4 - len(sig_b64) % 4) % 4)
    try:
        presented = base64.urlsafe_b64decode(sig_b64 + pad)
    except (ValueError, base64.binascii.Error):
        return False
    # Defense-in-depth length check — `compare_digest` already handles
    # mismatches in constant time, but pinning the expected length
    # makes the contract explicit and immune to a future truncation
    # change at the encode site.
    if len(presented) != 24:
        return False
    return hmac.compare_digest(expected, presented)


_LABELS_ZH = {
    "title": "看診摘要 — {name}",
    "period": "區間",
    "generated": "產生時間",
    "total_sightings": "出現次數",
    "days_seen": "看到天數",
    "avg_per_day": "平均 / 天",
    "longest_absence": "最長空窗",
    "hours": "小時",
    "cameras_seen": "出現過的攝影機",
    "no_cameras": "區間內沒有任何攝影機紀錄。",
    "sightings_word": "次",
    "chart_heading": "30 天活動趨勢",
    "litter_heading": "貓砂盆使用次數（14 天）",
    "litter_note": "一天進出很多次、或反覆快速進出，可能是泌尿道問題的早期警訊。",
    "water_heading": "水碗造訪次數（14 天）",
    "food_heading": "飯碗造訪次數（14 天）",
    "fight_heading": "激烈互動紀錄（30 天）",
    "fight_with": "對象",
    "fight_events": "事件數",
    "posture_heading": "姿勢 / 步態警示（30 天）",
    "posture_kind": "看到的狀況",
    "posture_kind_vomit": "可能要吐了",
    "posture_kind_gait": "走路怪怪的",
    "posture_detail": "什麼時候 / 哪台攝影機",
    "posture_note": "姿勢警示僅是提示飼主回頭看影片，不是診斷。",
    "col_when": "時間",
    "daily_breakdown": "每日活動（最近 30 天）",
    "col_date": "日期",
    "col_sightings": "次數",
    "col_cameras": "攝影機",
    "col_first": "最早",
    "col_last": "最晚",
    "notes": "飼主備註",
    "print_button": "列印 / 存成 PDF",
    "footer": "由 Pawcorder 從攝影機畫面整理而成，不能取代獸醫檢查。",
}
