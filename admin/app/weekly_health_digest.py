"""Weekly per-pet health digest pushed via Telegram.

Fires Sunday around 09:00 local-time, summarises the past 7 days for
every configured pet, and posts a single Telegram message. The digest
is meant to be a passive companion to the live alerter — owners read
it Sunday morning over coffee and notice patterns the per-anomaly
alerts miss (e.g. "Mochi's water sparkline has been gently dropping
for 4 days but never tripped a threshold").

Cadence reasoning:

  * **Weekly** — daily would erode the value (every alert turns into
    noise after a week). Weekly is the cadence vets recommend for
    chronic-condition tracking.
  * **Sunday morning** — Saturday tends to be busy, Monday's
    work-stress mode. Sunday morning is the most common "look at
    pet stuff" window for households we sampled.

OSS-friendly. The digest body is built from ``pet_health_overview``,
which itself uses every Pro detector that's present and silently
skips the rest. So a free-tier install gets a basic digest with
30-day activity + heatmap; a Pro install gets the same plus
litter / bowl / fight / posture sections.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from . import config_store, pet_health_overview, telegram as tg
from .utils import PollingTask

logger = logging.getLogger("pawcorder.weekly_health_digest")

# Poll every hour — small, cheap, and gives us hour-aligned dispatch
# without the complexity of a real cron loop. A short tick also makes
# clock-jump recovery automatic (e.g. after sleep wake-up).
POLL_INTERVAL_SECONDS = 60 * 60

# Local-time send window. We need a clear "fire-once-per-week" trigger
# that survives missed ticks (laptop sleep, container restart). The
# rule: any Sunday tick between 09:00 and 13:00 fires, debounced by
# week — so a container that started at 09:31 still sends within the
# Sunday-morning window owners expect, and one that started at 12:30
# still fires before the day's "morning" feeling has passed. The
# upper bound stops a Sunday-evening container start from sending a
# digest that should have gone out hours earlier.
FIRE_HOUR_START = 9
FIRE_HOUR_END = 13
FIRE_WEEKDAY = 6           # time.localtime().tm_wday — Sunday is 6


def _maps_emoji(score_label: str) -> str:
    """Picks a quick-read emoji per badge — keeps the Telegram preview
    glanceable on a phone-lock-screen without expanding."""
    return {"ok": "✅", "watch": "🟡", "alert": "🟠"}.get(score_label, "•")


def build_digest_message(*, now: Optional[float] = None) -> Optional[str]:
    """Compose the Telegram-flavoured HTML message body, or ``None`` if
    no pets are configured (nothing useful to send).

    Public so a /api/health/digest/preview route can render exactly
    what the next dispatch would send — admin "test" buttons, basically.
    """
    overviews = pet_health_overview.overview_for_all_pets(now=now)
    if not overviews:
        return None
    lines: list[str] = ["<b>Pawcorder weekly check-in</b>"]
    for ov in overviews:
        lines.append(
            f"\n{_maps_emoji(ov.score_label)} <b>{ov.pet_name}</b> · "
            f"score {int(ov.score)}"
        )
        # Activity stat — compute the median of the last 7 days from
        # the pre-built timeline (avoids re-reading sightings).
        recent = [d.total for d in ov.timeline_days[-7:]]
        if any(recent):
            avg = sum(recent) / len(recent)
            lines.append(f"  · ~{avg:.0f} sightings/day this week")
        # Behavior chip — surface the dominant non-idle behavior label
        # (resting / pacing / active / eating / drinking) when one
        # category dominated the day. Quiet days are skipped so the
        # digest doesn't start every line with a behavior badge.
        primary = (ov.behavior or {}).get("primary")
        primary_count = (ov.behavior or {}).get("counts", {}).get(primary, 0)
        primary_phrases = {
            "resting":  "mostly resting",
            "pacing":   "doing a lot of back-and-forth pacing",
            "active":   "running around a lot",
            "eating":   "spending time at the food bowl",
            "drinking": "spending time at the water bowl",
        }
        if primary in primary_phrases and primary_count > 0:
            lines.append(f"  · {primary_phrases[primary]}")
        # Surface the loud signals; quiet ones stay off the digest.
        if ov.absence_anomaly:
            lines.append("  · ⚠ not seen for 24h+")
        if ov.activity_anomaly:
            lines.append("  · ⚠ activity below baseline")
        if ov.litter_frequent:
            lines.append("  · ⚠ frequent litter-box visits — could be a urinary issue")
        if ov.litter_phantom:
            lines.append("  · ⚠ rapid in-and-out litter-box visits")
        if ov.bowl_drops:
            kinds = ", ".join(ov.bowl_drops)
            lines.append(f"  · ⚠ low bowl visits: {kinds}")
        if ov.bowl_silent:
            kinds = ", ".join(ov.bowl_silent)
            lines.append(f"  · ⚠ no bowl visits today: {kinds}")
        if ov.posture_flags:
            # Map internal kind tags to plain prose — owners see this,
            # not a debug log. Unknown keys fall back to the tag itself
            # so a future detector kind doesn't silently drop the line.
            posture_text = {
                "vomit": "looked like it was about to throw up",
                "gait":  "walking looked off",
            }
            phrases = [posture_text.get(k, k) for k in ov.posture_flags]
            lines.append("  · ⚠ worth a look: " + "; ".join(phrases))
        if ov.fight_pairs:
            pairs = ", ".join(ov.fight_pairs)
            lines.append(f"  · ⚠ rough interactions: {pairs}")
    lines.append(
        "\n<i>See the full charts in the admin under "
        "<b>Health</b>.</i>"
    )
    return "\n".join(lines)


# ---- dispatcher --------------------------------------------------------

class WeeklyHealthDigest(PollingTask):
    """Hourly poll, but only sends once per (year, week-of-year)."""

    name = "weekly-health-digest"
    interval_seconds = float(POLL_INTERVAL_SECONDS)

    def __init__(self) -> None:
        super().__init__()
        # Keyed by (year, iso-week) so a clock-set-back doesn't duplicate.
        self._sent_for: Optional[tuple[int, int]] = None

    @staticmethod
    def _within_window(now_local: time.struct_time) -> bool:
        """Sunday 09:00–10:00 inclusive on the start hour, exclusive
        on the end hour — feels natural to readers."""
        return (
            now_local.tm_wday == FIRE_WEEKDAY
            and FIRE_HOUR_START <= now_local.tm_hour < FIRE_HOUR_END
        )

    async def _tick(self) -> None:
        cfg = config_store.load_config()
        if not (cfg.telegram_enabled and cfg.telegram_bot_token and cfg.telegram_chat_id):
            return
        now = time.time()
        now_local = time.localtime(now)
        if not self._within_window(now_local):
            return
        # ISO calendar gives a year+week tuple; re-using the same key
        # across the year boundary is fine because the year changes too.
        key = (now_local.tm_year, time.strftime("%V", now_local))
        # Convert second element to int for stable comparison.
        key_int = (key[0], int(key[1]))
        if self._sent_for == key_int:
            return
        try:
            body = build_digest_message(now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("digest build failed: %s", exc)
            return
        if not body:
            # No pets — no point sending; mark sent so we don't retry
            # every hour for the rest of the day.
            self._sent_for = key_int
            return
        try:
            await tg.send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, body)
            self._sent_for = key_int
        except Exception as exc:  # noqa: BLE001
            # Don't latch the sent_for key — let the next tick retry.
            logger.warning("digest send failed: %s", exc)


scheduler = WeeklyHealthDigest()
