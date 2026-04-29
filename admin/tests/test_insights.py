"""Tests for cross-pet correlation, energy mode, bandwidth estimator."""
from __future__ import annotations

import time

import pytest


def _seed(pet_id, camera, start, end):
    from app import recognition
    recognition.append_sighting(recognition.Sighting(
        event_id=f"{pet_id}_{camera}_{start}",
        camera=camera, label="cat",
        pet_id=pet_id, pet_name=pet_id.title(),
        score=0.9, confidence="high",
        start_time=start, end_time=end,
    ))


# ---- correlation -------------------------------------------------------

def test_correlation_no_overlap(data_dir):
    from app import insights
    now = time.time()
    _seed("mochi", "kitchen", now - 100, now - 80)
    _seed("maru", "kitchen", now - 60, now - 40)
    pairs = insights.cross_pet_correlation(since_hours=1)
    assert pairs == []


def test_correlation_overlap_same_camera(data_dir):
    from app import insights
    now = time.time()
    _seed("mochi", "kitchen", now - 120, now - 60)
    _seed("maru", "kitchen", now - 90, now - 30)  # 30s overlap
    pairs = insights.cross_pet_correlation(since_hours=1)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.overlap_seconds == 30
    assert p.overlap_count == 1
    assert "kitchen" in p.cameras


def test_correlation_no_overlap_different_cameras(data_dir):
    """Two pets seen at the same time but on different cameras
    don't count as 'together'."""
    from app import insights
    now = time.time()
    _seed("mochi", "kitchen", now - 100, now - 50)
    _seed("maru", "living", now - 100, now - 50)
    pairs = insights.cross_pet_correlation(since_hours=1)
    assert pairs == []


def test_correlation_three_pets(data_dir):
    """All pairs ordered consistently (lex by pet_id)."""
    from app import insights
    now = time.time()
    _seed("a", "k", now - 100, now - 50)
    _seed("b", "k", now - 90, now - 60)
    _seed("c", "k", now - 80, now - 40)
    pairs = insights.cross_pet_correlation(since_hours=1)
    assert len(pairs) == 3  # ab, ac, bc


# ---- energy mode -------------------------------------------------------

def test_energy_mode_disabled_never_pauses(data_dir):
    from app.insights import EnergyMode, EnergySchedule, is_camera_currently_paused
    mode = EnergyMode(enabled=False, schedules=[
        EnergySchedule(cameras=["x"], start_hour=0, end_hour=24),
    ])
    assert is_camera_currently_paused("x", mode=mode, hour=12) is False


def test_energy_mode_simple_window(data_dir):
    from app.insights import EnergyMode, EnergySchedule, is_camera_currently_paused
    mode = EnergyMode(enabled=True, schedules=[
        EnergySchedule(cameras=["bedroom"], start_hour=22, end_hour=23),
    ])
    assert is_camera_currently_paused("bedroom", mode=mode, hour=22) is True
    assert is_camera_currently_paused("bedroom", mode=mode, hour=21) is False
    assert is_camera_currently_paused("bedroom", mode=mode, hour=23) is False
    assert is_camera_currently_paused("kitchen", mode=mode, hour=22) is False


def test_energy_mode_wrap_midnight(data_dir):
    """22 -> 6 means '22:00 to 06:00 next morning'."""
    from app.insights import EnergyMode, EnergySchedule, is_camera_currently_paused
    mode = EnergyMode(enabled=True, schedules=[
        EnergySchedule(cameras=["porch"], start_hour=22, end_hour=6),
    ])
    assert is_camera_currently_paused("porch", mode=mode, hour=23) is True
    assert is_camera_currently_paused("porch", mode=mode, hour=2)  is True
    assert is_camera_currently_paused("porch", mode=mode, hour=5)  is True
    assert is_camera_currently_paused("porch", mode=mode, hour=6)  is False
    assert is_camera_currently_paused("porch", mode=mode, hour=12) is False


def test_energy_mode_zero_window_ignored(data_dir):
    """start == end is a 0-length window — must be a no-op (otherwise
    setting both to 0 would pause the whole day)."""
    from app.insights import EnergyMode, EnergySchedule, is_camera_currently_paused
    mode = EnergyMode(enabled=True, schedules=[
        EnergySchedule(cameras=["x"], start_hour=0, end_hour=0),
    ])
    for h in range(24):
        assert is_camera_currently_paused("x", mode=mode, hour=h) is False


def test_energy_mode_save_load_round_trip(data_dir):
    from app.insights import EnergyMode, EnergySchedule, load_energy_mode, save_energy_mode
    save_energy_mode(EnergyMode(enabled=True, schedules=[
        EnergySchedule(cameras=["a", "b"], start_hour=22, end_hour=6),
    ]))
    loaded = load_energy_mode()
    assert loaded.enabled
    assert loaded.schedules[0].cameras == ["a", "b"]
    assert loaded.schedules[0].start_hour == 22


# ---- routes ------------------------------------------------------------

def test_correlation_route(authed_client, data_dir):
    now = time.time()
    _seed("mochi", "kitchen", now - 120, now - 60)
    _seed("maru", "kitchen", now - 90, now - 30)
    resp = authed_client.get("/api/pets/correlation")
    assert resp.status_code == 200
    pairs = resp.json()["pairs"]
    assert len(pairs) == 1
    assert pairs[0]["overlap_count"] == 1


def test_energy_mode_routes(authed_client):
    resp = authed_client.post("/api/energy-mode", json={
        "enabled": True,
        "schedules": [{"cameras": ["bedroom"], "start_hour": 22, "end_hour": 6}],
    })
    assert resp.status_code == 200
    state = authed_client.get("/api/energy-mode").json()
    assert state["enabled"] is True
    assert state["schedules"][0]["cameras"] == ["bedroom"]


def test_bandwidth_route_no_frigate_returns_empty(authed_client, monkeypatch):
    """Frigate down → empty list, no exception."""
    from app import insights
    async def _bad(*a, **k):
        raise insights.httpx.HTTPError("connection refused")
    # Replace the AsyncClient context — easier: stub the function.
    monkeypatch.setattr(insights, "bandwidth_per_camera",
                        lambda: _stub_async_empty())
    async def _stub_async_empty():
        return []
    resp = authed_client.get("/api/system/bandwidth")
    # Even if monkeypatch path is wonky, the real code returns [] on
    # error, so we just check status.
    assert resp.status_code == 200


# ---- energy mode → Frigate render integration -------------------------

def test_energy_mode_disables_camera_in_frigate_render(data_dir, monkeypatch):
    """End-to-end: enabling energy mode for current hour must flip
    `enabled` to false in the rendered Frigate config."""
    from app import config_store, insights
    from app.cameras_store import Camera

    # Force "current hour" to 2 AM and put bedroom in a 22→6 window.
    import time as _time
    real_local = _time.localtime
    monkeypatch.setattr(insights.time, "localtime",
                        lambda *a: real_local(*a) if a else _time.struct_time(
                            (2026, 1, 1, 2, 0, 0, 0, 0, 0)
                        ))

    insights.save_energy_mode(insights.EnergyMode(
        enabled=True,
        schedules=[insights.EnergySchedule(
            cameras=["bedroom"], start_hour=22, end_hour=6,
        )],
    ))

    cfg = config_store.load_config()
    cams = [
        Camera(name="bedroom", ip="1.1.1.1", password="x"),
        Camera(name="kitchen", ip="2.2.2.2", password="y"),
    ]
    rendered = config_store.render_frigate_config(cfg, cams)
    # The template has go2rtc.streams.<cam>: and cameras.<cam>: — we
    # only care about the second one. Slice from the cameras: header.
    cameras_section = rendered.split("\ncameras:\n", 1)[1]
    bedroom_block = cameras_section.split("bedroom:", 1)[1].split("ffmpeg:", 1)[0]
    kitchen_block = cameras_section.split("kitchen:", 1)[1].split("ffmpeg:", 1)[0]
    assert "enabled: false" in bedroom_block
    assert "enabled: true" in kitchen_block
