"""Tests for camera brand compatibility matrix + RTSP path templating."""
from __future__ import annotations


def test_brands_listing_route(authed_client):
    resp = authed_client.get("/api/camera-brands")
    assert resp.status_code == 200
    brands = resp.json()["brands"]
    keys = {b["key"] for b in brands}
    # Sanity: the brands we promised to support are all listed.
    assert {"reolink", "tapo", "hikvision", "dahua", "amcrest", "other"} <= keys


def test_brand_metadata_shape(data_dir):
    from app import camera_compat
    for b in camera_compat.list_brands():
        assert b["name"]
        assert b["rtsp_main"].startswith("rtsp://")
        assert b["rtsp_sub"].startswith("rtsp://")
        assert isinstance(b["two_way_audio_supported"], bool)


def test_unknown_brand_falls_back_to_other(data_dir):
    from app import camera_compat
    spec = camera_compat.get_brand("frobnicator")
    assert spec.name.startswith("Other")


def test_camera_template_view_uses_brand_paths(data_dir):
    from app.cameras_store import Camera
    cam = Camera(name="cam", ip="1.2.3.4", password="p", brand="tapo")
    view = cam.template_view()
    # Tapo uses /stream1 + /stream2, not /h264Preview_01_main.
    assert view["rtsp_main_path"] == "/stream1"
    assert view["rtsp_sub_path"] == "/stream2"


def test_camera_template_view_default_reolink(data_dir):
    from app.cameras_store import Camera
    cam = Camera(name="cam", ip="1.2.3.4", password="p")
    view = cam.template_view()
    assert "h264Preview_01_main" in view["rtsp_main_path"]


def test_unifi_uses_port_7447(data_dir):
    from app import camera_compat
    spec = camera_compat.get_brand("ubiquiti")
    assert spec.default_rtsp_port == 7447


def test_unifi_is_manual_setup(data_dir):
    """Round-4 C1: until the controller-flow ships, UniFi must be flagged
    manual_setup so the dispatcher short-circuits and the cameras-page UI
    shows the in-app guidance instead of falling through to ONVIF (which
    fails on Protect's port-80-less RTSP-only model)."""
    from app import camera_compat
    spec = camera_compat.get_brand("ubiquiti")
    assert spec.manual_setup is True


def test_camera_brands_route_emits_setup_steps(authed_client):
    """The /api/camera-brands route post-processes list_brands() output to
    attach translated `setup_title` + `setup_steps` for each manual brand
    (and the "other" catch-all). The cameras-page template iterates these
    with a single x-for — so a missing payload here means a missing UI
    panel for users picking that brand."""
    resp = authed_client.get("/api/camera-brands")
    assert resp.status_code == 200
    by_key = {b["key"]: b for b in resp.json()["brands"]}
    for key in ("tapo", "imou", "wyze", "ubiquiti"):
        b = by_key[key]
        assert b["manual_setup"] is True, f"{key} should be manual_setup"
        assert b["setup_title"], f"{key} missing setup_title"
        assert isinstance(b["setup_steps"], list)
        assert len(b["setup_steps"]) >= 3, f"{key} should have >=3 setup steps, got {b['setup_steps']!r}"
        for step in b["setup_steps"]:
            assert isinstance(step, str) and step
    # "other" isn't manual but still has a guidance panel.
    assert by_key["other"]["setup_title"]
    assert len(by_key["other"]["setup_steps"]) >= 1
    # Reolink has no manual panel — it's fully automatic. Empty payload is OK.
    assert by_key["reolink"]["setup_steps"] == []
    assert by_key["reolink"]["setup_title"] == ""


def test_path_only_helper(data_dir):
    from app.cameras_store import _path_only
    assert _path_only("rtsp://USER:PASS@IP:554/stream1") == "/stream1"
    assert _path_only("rtsp://USER:PASS@IP:7447/A/B/C") == "/A/B/C"
    assert _path_only("rtsp://USER:PASS@IP:554/") == "/"


def test_camera_create_with_brand_and_two_way(authed_client):
    resp = authed_client.post("/api/cameras", json={
        "name": "patio",
        "ip": "1.2.3.4",
        "password": "p",
        "brand": "amcrest",
        "two_way_audio": True,
    })
    assert resp.status_code == 200
    got = authed_client.get("/api/cameras/patio").json()["camera"]
    assert got["brand"] == "amcrest"
    assert got["two_way_audio"] is True


def test_frigate_template_renders_with_brand_specific_path(data_dir):
    """End-to-end: a Tapo camera renders /stream1 in the Frigate yaml."""
    from app import config_store
    from app.cameras_store import Camera

    cfg = config_store.load_config()
    cams = [Camera(name="patio", ip="1.2.3.4", password="p", brand="tapo")]
    rendered = config_store.render_frigate_config(cfg, cams)
    assert "/stream1" in rendered
    assert "/stream2" in rendered
    assert "h264Preview_01_main" not in rendered


def test_two_way_audio_adds_backchannel_flag(data_dir):
    from app import config_store
    from app.cameras_store import Camera

    cfg = config_store.load_config()
    cams = [Camera(name="cam", ip="1.2.3.4", password="p",
                   brand="reolink", two_way_audio=True)]
    rendered = config_store.render_frigate_config(cfg, cams)
    assert "#backchannel=1" in rendered


def test_two_way_audio_off_no_backchannel(data_dir):
    from app import config_store
    from app.cameras_store import Camera

    cfg = config_store.load_config()
    cams = [Camera(name="cam", ip="1.2.3.4", password="p",
                   brand="reolink", two_way_audio=False)]
    rendered = config_store.render_frigate_config(cfg, cams)
    assert "#backchannel=1" not in rendered
