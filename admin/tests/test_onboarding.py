"""Tests for the dashboard onboarding tracker."""
from __future__ import annotations

from app import onboarding
from app.config_store import Config
from app.privacy import PrivacyState


def _bare_cfg(**overrides) -> Config:
    return Config(**overrides)


def _state(cfg, pets=None, privacy=None):
    return onboarding.get_state(cfg, pets or [], privacy or PrivacyState())


def test_fresh_install_has_zero_complete_and_first_step_pending(data_dir):
    state = _state(_bare_cfg())
    assert state["completed_count"] == 0
    assert state["skipped_count"] == 0
    assert state["all_done"] is False
    assert state["next_step_key"] == "notifications"
    assert state["next_step_href"] == "/notifications"


def test_telegram_complete_requires_token_AND_chat_id(data_dir):
    """A bot token alone isn't enough — without chat_id the bot
    can't actually deliver. Tightened from the original "any token
    counts" check after a review pointed out the half-configured
    case silently fails at send time."""
    # Token alone — not done.
    cfg = _bare_cfg(telegram_enabled=True, telegram_bot_token="bot:xyz")
    state = _state(cfg)
    assert next(s for s in state["steps"] if s["key"] == "notifications")["completed"] is False
    # Token + chat_id — done.
    cfg = _bare_cfg(telegram_enabled=True, telegram_bot_token="bot:xyz", telegram_chat_id="987")
    state = _state(cfg)
    assert state["completed_count"] == 1
    assert next(s for s in state["steps"] if s["key"] == "notifications")["completed"] is True
    assert state["next_step_key"] == "ai_token"


def test_line_complete_requires_token_AND_target(data_dir):
    cfg = _bare_cfg(line_enabled=True, line_channel_token="line:xyz")
    state = _state(cfg)
    assert next(s for s in state["steps"] if s["key"] == "notifications")["completed"] is False
    cfg = _bare_cfg(line_enabled=True, line_channel_token="line:xyz", line_target_id="U123")
    state = _state(cfg)
    assert next(s for s in state["steps"] if s["key"] == "notifications")["completed"] is True


def test_telegram_enabled_without_token_does_not_count(data_dir):
    cfg = _bare_cfg(telegram_enabled=True, telegram_bot_token="")
    state = _state(cfg)
    assert next(s for s in state["steps"] if s["key"] == "notifications")["completed"] is False


def test_openai_key_or_pro_license_completes_ai_token(data_dir):
    assert _state(_bare_cfg(openai_api_key="sk-..."))["completed_count"] >= 1
    assert _state(_bare_cfg(pawcorder_pro_license_key="pro_..."))["completed_count"] >= 1


def test_pets_step_counts_when_pets_list_non_empty(data_dir):
    pets = [{"id": "mochi", "name": "Mochi"}]
    state = _state(_bare_cfg(), pets=pets)
    assert next(s for s in state["steps"] if s["key"] == "pets")["completed"] is True


def test_privacy_step_counts_when_state_enabled(data_dir):
    state = _state(_bare_cfg(), privacy=PrivacyState(enabled=True))
    assert next(s for s in state["steps"] if s["key"] == "privacy_mode")["completed"] is True


def test_remote_access_requires_tailscale_hostname(data_dir):
    """Pro license alone does NOT count — Connect still has to be
    activated by the user. Granting completion on license-alone would
    defeat the widget's purpose (nudging the user to finish setup)."""
    assert next(
        s for s in _state(_bare_cfg(tailscale_hostname="paw.tail-x.ts.net"))["steps"]
        if s["key"] == "remote_access"
    )["completed"] is True
    # Pro license alone — pending.
    assert next(
        s for s in _state(_bare_cfg(pawcorder_pro_license_key="pro_..."))["steps"]
        if s["key"] == "remote_access"
    )["completed"] is False


def test_cloud_backup_needs_both_enabled_and_backend(data_dir):
    # enabled but no backend selected — not done
    cfg = _bare_cfg(cloud_enabled=True, cloud_backend="")
    state = _state(cfg)
    assert next(s for s in state["steps"] if s["key"] == "cloud_backup")["completed"] is False
    # backend without enabled — also not done
    cfg = _bare_cfg(cloud_enabled=False, cloud_backend="drive")
    state = _state(cfg)
    assert next(s for s in state["steps"] if s["key"] == "cloud_backup")["completed"] is False
    # both — done
    cfg = _bare_cfg(cloud_enabled=True, cloud_backend="drive")
    state = _state(cfg)
    assert next(s for s in state["steps"] if s["key"] == "cloud_backup")["completed"] is True


def test_skip_step_removes_it_from_pending_but_not_completed(data_dir):
    onboarding.skip_step("notifications")
    state = _state(_bare_cfg())
    notifications = next(s for s in state["steps"] if s["key"] == "notifications")
    assert notifications["completed"] is False
    assert notifications["skipped"] is True
    assert state["skipped_count"] == 1
    assert state["next_step_key"] == "ai_token"


def test_skip_step_unknown_key_raises(data_dir):
    import pytest
    with pytest.raises(ValueError, match="unknown onboarding step"):
        onboarding.skip_step("not-a-step")


def test_skip_all_marks_widget_done(data_dir):
    onboarding.skip_all()
    state = _state(_bare_cfg())
    assert state["all_done"] is True
    assert state["next_step_key"] is None
    # All steps should report skipped (since none are completed in a bare cfg).
    assert all(s["skipped"] for s in state["steps"])


def test_completed_step_overrides_skipped(data_dir):
    """A user can skip notifications, then later set them up — the
    widget should consider it complete (not skipped). That keeps the
    derived-from-config invariant honest."""
    onboarding.skip_step("notifications")
    cfg = _bare_cfg(telegram_enabled=True, telegram_bot_token="bot:abc",
                    telegram_chat_id="987")
    state = _state(cfg)
    notifications = next(s for s in state["steps"] if s["key"] == "notifications")
    assert notifications["completed"] is True
    assert notifications["skipped"] is False


def test_reset_clears_skip_set(data_dir):
    onboarding.skip_all()
    assert _state(_bare_cfg())["all_done"] is True
    onboarding.reset()
    assert _state(_bare_cfg())["all_done"] is False


def test_skip_persists_across_loads(data_dir):
    onboarding.skip_step("cloud_backup")
    # Round-trip via load_skipped (simulates a fresh process boot).
    skipped = onboarding.load_skipped()
    assert skipped == {"cloud_backup"}


def test_get_state_attaches_translated_title_and_why_when_translator_passed(data_dir):
    """Server-side translation removes the per-step lookup table the
    dashboard template used to maintain in JS — adding a new step is a
    single-place change."""
    def translator(key: str) -> str:
        return f"[{key}]"  # deterministic stub
    state = onboarding.get_state(_bare_cfg(), [], None, translator=translator)
    notif = next(s for s in state["steps"] if s["key"] == "notifications")
    assert notif["title"] == "[ONBOARDING_STEP_NOTIFICATIONS_TITLE]"
    assert notif["why"]   == "[ONBOARDING_STEP_NOTIFICATIONS_WHY]"


def test_get_state_omits_title_and_why_when_no_translator(data_dir):
    state = onboarding.get_state(_bare_cfg(), [], None)
    notif = next(s for s in state["steps"] if s["key"] == "notifications")
    assert "title" not in notif
    assert "why" not in notif
