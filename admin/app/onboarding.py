"""Post-setup-wizard onboarding tracker.

The 5-step setup wizard at `/setup` covers the bare minimum to get
Frigate streaming. This module covers everything *after* that — the
features users tend to forget exist if they're not nudged: Telegram /
LINE notifications, cloud backup, privacy mode, pet identity photos,
AI token (OSS OpenAI key or Pro license), and remote access.

State is *derived* from real config rather than tracked separately:
the dashboard widget asks `get_state()`, which inspects the actual
`Config`, `pets.yml`, and `privacy.json` to decide which steps look
done. The only thing this module persists is the user's *skipped* set
— if they explicitly hide a step, we honour that across page loads.

Why deriving rather than tracking-by-button: a user who finished
notification setup via the /notifications page gets ticked off
automatically the next time the dashboard renders, with no need to
remember to call a "mark complete" hook from inside that route.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("pawcorder.onboarding")

# `_lock` serialises read-modify-write on the skip file so two parallel
# POST /api/onboarding/skip requests can't lose each other's update.
_lock = threading.Lock()

def _skip_path() -> Path:
    """Resolved at call time — tests swap PAWCORDER_DATA_DIR per fixture
    via monkeypatch, so freezing the path at import time bakes in /data
    and breaks isolation."""
    return Path(os.environ.get("PAWCORDER_DATA_DIR", "/data")) / "config" / "onboarding_skipped.json"


# --- step definitions -----------------------------------------------------
#
# Order matters — this is the order the dashboard widget walks the user
# through. Most-impactful first; remote access last because most users
# initially watch from home.

# Each step's "is this done?" predicate is stored alongside the step itself
# so adding a new step is a single dataclass-instance change rather than
# adding a clause to a stringly-typed dispatch.
@dataclass(frozen=True)
class _StepDef:
    key: str                      # stable identifier (i18n + skip persistence)
    href: str                     # admin route the dashboard widget links to
    is_complete: Callable[[Any, list, Any], bool]


def _check_notifications(cfg: Any, _pets: list, _priv: Any) -> bool:
    # Token alone isn't enough — a chat/user id is needed for the bot to
    # actually deliver messages. Otherwise the widget vanishes for a
    # half-configured setup that silently fails.
    return ((cfg.telegram_enabled and bool(cfg.telegram_bot_token) and bool(cfg.telegram_chat_id))
            or (cfg.line_enabled and bool(cfg.line_channel_token) and bool(cfg.line_target_id)))


def _check_ai_token(cfg: Any, _pets: list, _priv: Any) -> bool:
    return bool(cfg.openai_api_key) or bool(cfg.pawcorder_pro_license_key)


def _check_cloud_backup(cfg: Any, _pets: list, _priv: Any) -> bool:
    return cfg.cloud_enabled and bool(cfg.cloud_backend)


def _check_pets(_cfg: Any, pets: list, _priv: Any) -> bool:
    return len(pets) > 0


def _check_privacy_mode(_cfg: Any, _pets: list, priv: Any) -> bool:
    return bool(getattr(priv, "enabled", False))


def _check_remote_access(cfg: Any, _pets: list, _priv: Any) -> bool:
    # Tailscale hostname being set means the user did the wiring. Owning
    # a Pro license alone is NOT enough — pawcorder Connect still has to
    # be activated; granting completion on license-alone would defeat
    # the widget's job (nudging the user to finish setup).
    return bool(cfg.tailscale_hostname)


STEPS: list[_StepDef] = [
    _StepDef("notifications", "/notifications", _check_notifications),
    _StepDef("ai_token",      "/system",        _check_ai_token),
    _StepDef("cloud_backup",  "/cloud",         _check_cloud_backup),
    _StepDef("pets",          "/pets",          _check_pets),
    _StepDef("privacy_mode",  "/privacy",       _check_privacy_mode),
    _StepDef("remote_access", "/mobile",        _check_remote_access),
]


# --- persistence (skip set only) ------------------------------------------

def load_skipped() -> set[str]:
    if not _skip_path().exists():
        return set()
    try:
        data = json.loads(_skip_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get("skipped", []))


def save_skipped(skipped: set[str]) -> None:
    from .utils import atomic_write_text
    path = _skip_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps({"skipped": sorted(skipped)}, ensure_ascii=False, indent=2),
        )
    except OSError as exc:  # data-dir misconfigured — skip-set is non-critical UX state
        logger.warning("could not persist onboarding skip set to %s: %s", path, exc)


def skip_step(key: str) -> None:
    """Hide one step from the dashboard widget."""
    if key not in {s.key for s in STEPS}:
        raise ValueError(f"unknown onboarding step: {key!r}")
    # Lock around read-modify-write — without this two parallel POSTs
    # could each load the same baseline and one update would be lost.
    with _lock:
        skipped = load_skipped()
        if key not in skipped:
            skipped.add(key)
            save_skipped(skipped)


def skip_all() -> None:
    """Hide the widget entirely — user clicked "later, thanks"."""
    with _lock:
        save_skipped({s.key for s in STEPS})


def reset() -> None:
    """Wipe the skip set so the widget reappears. Test/QA helper."""
    with _lock:
        path = _skip_path()
        if path.exists():
            path.unlink()


# --- state computation ----------------------------------------------------

def get_state(
    cfg: Any, pets: list, privacy_state: Any,
    translator: Callable[[str], str] | None = None,
) -> dict:
    """Return the dashboard payload describing onboarding progress.

    `translator` is the bound `i18n.t(lang)` for the request locale —
    when provided, each step's `title` and `why` are pre-translated so
    the dashboard template doesn't need to keep its own per-step lookup
    table in sync with the Python step list. Tests call without the arg.
    """
    skipped = load_skipped()
    steps_payload = []
    next_pending: _StepDef | None = None
    completed_count = 0
    skipped_count = 0

    for step in STEPS:
        completed = step.is_complete(cfg, pets, privacy_state)
        is_skipped = (not completed) and (step.key in skipped)
        if completed:
            completed_count += 1
        elif is_skipped:
            skipped_count += 1
        elif next_pending is None:
            next_pending = step
        entry = {
            "key": step.key,
            "href": step.href,
            "completed": completed,
            "skipped": is_skipped,
        }
        if translator is not None:
            upper = step.key.upper()
            entry["title"] = translator(f"ONBOARDING_STEP_{upper}_TITLE")
            entry["why"]   = translator(f"ONBOARDING_STEP_{upper}_WHY")
        steps_payload.append(entry)

    return {
        "steps": steps_payload,
        "completed_count": completed_count,
        "skipped_count": skipped_count,
        "total": len(STEPS),
        "next_step_key": next_pending.key if next_pending else None,
        "next_step_href": next_pending.href if next_pending else None,
        "all_done": next_pending is None,
    }
