"""Tests for .env IO and Frigate template rendering."""
from __future__ import annotations

import pytest
import yaml


def test_load_defaults_when_env_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PAWCORDER_DATA_DIR", str(tmp_path))
    import sys
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            sys.modules.pop(mod, None)
    from app import config_store
    cfg = config_store.load_config()
    assert cfg.tz == "Asia/Taipei"
    assert cfg.pet_min_score == "0.65"


def test_write_env_is_atomic(data_dir, monkeypatch):
    """Crash between write and rename must leave the previous .env intact.

    .env holds ADMIN_PASSWORD and ADMIN_SESSION_SECRET — a torn write
    locks the user out of the admin panel entirely, requiring SSH
    recovery. atomic_write_text via .writing+os.replace prevents this.
    """
    from app import config_store
    from app import utils

    # Ensure a known-good .env exists (the conftest fixture wrote one,
    # but we want a fresh round-trip to be sure).
    cfg = config_store.load_config()
    cfg.admin_password = "original-password"
    cfg.admin_session_secret = "original-secret"
    config_store.save_config(cfg)

    # Now sabotage os.replace inside utils so the next write crashes.
    def _boom(*_args, **_kw):
        raise OSError("simulated kill between write and rename")
    monkeypatch.setattr(utils.os, "replace", _boom)

    cfg.admin_password = "GARBAGE"
    cfg.admin_session_secret = "GARBAGE"
    with pytest.raises(OSError):
        config_store.save_config(cfg)

    # The .env on disk must still hold the original values — atomic guarantee.
    survived = config_store.load_config()
    assert survived.admin_password == "original-password"
    assert survived.admin_session_secret == "original-secret"


def test_round_trip_preserves_values(data_dir):
    from app import config_store
    cfg = config_store.load_config()
    cfg.tz = "America/New_York"
    cfg.pet_min_score = "0.72"
    cfg.telegram_enabled = True
    cfg.telegram_bot_token = "abc:123"
    cfg.line_target_id = "U" + "x" * 32
    cfg.track_cat = False
    config_store.save_config(cfg)

    fresh = config_store.load_config()
    assert fresh.tz == "America/New_York"
    assert fresh.pet_min_score == "0.72"
    assert fresh.telegram_enabled is True
    assert fresh.telegram_bot_token == "abc:123"
    assert fresh.line_target_id == "U" + "x" * 32
    assert fresh.track_cat is False


def test_password_with_quotes_round_trips(data_dir):
    from app import config_store
    cfg = config_store.load_config()
    cfg.frigate_rtsp_password = 'with"quote\\backslash'
    config_store.save_config(cfg)
    assert config_store.load_config().frigate_rtsp_password == 'with"quote\\backslash'


def test_render_frigate_two_cameras(data_dir):
    from app import config_store
    from app.cameras_store import Camera

    cfg = config_store.load_config()
    cams = [
        Camera(name="living_room", ip="192.168.1.100", password='p@ss"word', user="admin"),
        Camera(name="kitchen",     ip="192.168.1.101", password="simple",     user="admin"),
    ]
    text = config_store.render_frigate_config(cfg, cams)
    parsed = yaml.safe_load(text)
    assert set(parsed["cameras"].keys()) == {"living_room", "kitchen"}
    # 4 streams: 2 cameras × (main + sub)
    assert set(parsed["go2rtc"]["streams"].keys()) == {
        "living_room", "living_room_sub", "kitchen", "kitchen_sub",
    }
    # Special chars URL-encoded in stream URL
    main_url = parsed["go2rtc"]["streams"]["living_room"][0]
    assert "%40ss%22word" in main_url
    # Bare password kept for ONVIF
    assert parsed["cameras"]["living_room"]["onvif"]["password"] == 'p@ss"word'


def test_render_skips_disabled_species(data_dir):
    from app import config_store
    from app.cameras_store import Camera

    cfg = config_store.load_config()
    cfg.track_cat = False
    cfg.track_dog = True
    cfg.track_person = False
    cams = [Camera(name="cam", ip="1.1.1.1", password="x")]
    text = config_store.render_frigate_config(cfg, cams)
    parsed = yaml.safe_load(text)
    track = parsed["cameras"]["cam"]["objects"]["track"]
    assert track == ["dog"]
    filters = parsed["cameras"]["cam"]["objects"]["filters"]
    assert "cat" not in filters and "person" not in filters and "dog" in filters
    review = parsed["cameras"]["cam"]["review"]
    assert review.get("alerts", {}).get("labels") == ["dog"]
    assert "detections" not in review  # only person triggers detections section


@pytest.mark.parametrize("combo", [
    {"track_cat": True, "track_dog": True, "track_person": True},
    {"track_cat": True, "track_dog": False, "track_person": False},
    {"track_cat": False, "track_dog": False, "track_person": True},
    {"track_cat": True, "track_dog": True, "track_person": False},
])
def test_render_yaml_valid_for_all_combos(data_dir, combo):
    from app import config_store
    from app.cameras_store import Camera
    cfg = config_store.load_config()
    for k, v in combo.items():
        setattr(cfg, k, v)
    cams = [Camera(name="cam", ip="1.1.1.1", password="x")]
    parsed = yaml.safe_load(config_store.render_frigate_config(cfg, cams))
    assert parsed["cameras"]["cam"]["enabled"] is True


def test_is_setup_complete(data_dir):
    from app import config_store
    from app.cameras_store import Camera
    cfg = config_store.load_config()
    assert config_store.is_setup_complete(cfg, []) is False
    cams = [Camera(name="cam", ip="1.1.1.1", password="x")]
    assert config_store.is_setup_complete(cfg, cams) is True
    cfg.storage_path = ""
    assert config_store.is_setup_complete(cfg, cams) is False


def test_render_and_write_skips_when_no_cameras(data_dir):
    from app import config_store
    assert config_store.render_and_write_if_complete() is False
    assert not config_store.RENDERED_PATH.exists()


def test_random_password_distinct_and_alnum():
    from app.config_store import random_password
    a, b = random_password(), random_password()
    assert a != b
    assert len(a) == 24
    assert a.isalnum()


@pytest.mark.parametrize("detector,expect_in_yaml", [
    ("openvino", "openvino"),
    ("tensorrt", "tensorrt"),
    ("edgetpu",  "edgetpu"),
    ("hailo8l",  "hailo8l"),
    ("cpu",      "cpu"),
])
def test_render_picks_correct_detector(data_dir, detector, expect_in_yaml):
    from app import config_store
    from app.cameras_store import Camera
    cfg = config_store.load_config()
    cfg.detector_type = detector
    cams = [Camera(name="cam", ip="1.1.1.1", password="x")]
    out = config_store.render_frigate_config(cfg, cams)
    parsed = yaml.safe_load(out)
    assert parsed.get("detectors"), f"no detectors block for {detector}"
    detector_types = {v.get("type") for v in parsed["detectors"].values() if isinstance(v, dict)}
    assert expect_in_yaml in detector_types


def test_detector_type_round_trips_in_env(data_dir):
    from app import config_store
    cfg = config_store.load_config()
    cfg.detector_type = "openvino"
    config_store.save_config(cfg)
    assert config_store.load_config().detector_type == "openvino"
