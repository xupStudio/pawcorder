"""End-to-end smoke tests against the FastAPI app via TestClient.

External dependencies (Docker, Reolink, Telegram, LINE, nmap) are stubbed
in the conftest fixtures.
"""
from __future__ import annotations

import pytest


# ---- auth flow ----------------------------------------------------------

def test_login_page_renders(app_client):
    resp = app_client.get("/login")
    assert resp.status_code == 200
    assert "pawcorder" in resp.text


def test_unauthenticated_redirects_to_login(app_client):
    resp = app_client.get("/cameras", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_unauthenticated_api_returns_401(app_client):
    resp = app_client.get("/api/status")
    assert resp.status_code == 401
    assert resp.json() == {"error": "not_authenticated"}


def test_login_wrong_password(app_client):
    resp = app_client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=invalid" in resp.headers["location"]


def test_login_then_dashboard(authed_client):
    resp = authed_client.get("/", follow_redirects=False)
    # No cameras yet, so dashboard redirects to /setup.
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


# ---- pages render -------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/setup",
    "/cameras",
    "/detection",
    "/storage",
    "/system",
    "/mobile",
    "/notifications",
    "/home-assistant",
    "/hardware",
    "/cloud",
])
def test_authed_pages_200(authed_client, path):
    resp = authed_client.get(path)
    assert resp.status_code == 200
    assert "pawcorder" in resp.text


# ---- cameras CRUD -------------------------------------------------------

def test_cameras_crud_full_flow(authed_client):
    # initial: empty
    assert authed_client.get("/api/cameras").json() == {"cameras": []}

    # create
    resp = authed_client.post("/api/cameras", json={
        "name": "living_room", "ip": "192.168.1.100", "password": "x",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # list
    cams = authed_client.get("/api/cameras").json()["cameras"]
    assert len(cams) == 1
    assert cams[0]["name"] == "living_room"

    # auto-detected connection_type from stub_reolink
    assert cams[0]["connection_type"] == "wired"

    # update — changes IP
    resp = authed_client.put("/api/cameras/living_room", json={
        "name": "living_room", "ip": "192.168.1.123",
    })
    assert resp.status_code == 200
    assert authed_client.get("/api/cameras/living_room").json()["camera"]["ip"] == "192.168.1.123"

    # delete
    resp = authed_client.delete("/api/cameras/living_room")
    assert resp.status_code == 200
    assert authed_client.get("/api/cameras").json() == {"cameras": []}


def test_camera_create_rejects_invalid_name(authed_client):
    resp = authed_client.post("/api/cameras", json={
        "name": "Living Room", "ip": "1.1.1.1", "password": "x",
    })
    assert resp.status_code == 400
    assert "name" in resp.json()["error"].lower()


def test_camera_create_then_status_setup_complete(authed_client):
    authed_client.post("/api/cameras", json={
        "name": "cam", "ip": "1.1.1.1", "password": "x",
    })
    status = authed_client.get("/api/status").json()
    assert status["setup_complete"] is True
    assert status["camera_count"] == 1


def test_camera_test_endpoint(authed_client):
    resp = authed_client.post("/api/cameras/test", json={
        "ip": "192.168.1.100", "password": "x",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["connection_type"] == "wired"


def test_camera_test_wifi_classification(authed_client):
    resp = authed_client.post("/api/cameras/test", json={
        "ip": "192.168.1.101", "password": "x",  # stub returns wifi for .101
    })
    assert resp.json()["connection_type"] == "wifi"


def test_camera_test_endpoint_routes_brand_through_dispatcher(authed_client):
    """Non-Reolink brand goes through stub_camera_dispatcher (deterministic
    shape) instead of camera_api.auto_configure."""
    resp = authed_client.post("/api/cameras/test", json={
        "brand": "hikvision", "ip": "10.0.0.1", "user": "admin", "password": "x",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["brand"] == "hikvision"
    assert body["connection_type"] == "wired"
    assert body["reolink_login"]["ok"] is True


def test_camera_test_endpoint_manual_brand_returns_sentinel(authed_client):
    """Manual-setup brands (Tapo) report manual=True so the UI can render
    the per-brand step-by-step instructions instead of treating it as a
    failed login."""
    resp = authed_client.post("/api/cameras/test", json={
        "brand": "tapo", "ip": "10.0.0.2", "user": "admin", "password": "x",
    })
    assert resp.status_code == 200
    assert resp.json()["reolink_login"]["manual"] is True


# ---- detection toggles --------------------------------------------------

def test_detection_save_with_one_species(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "detection",
        "data": {"track_cat": True, "track_dog": False, "track_person": False},
    })
    assert resp.status_code == 200


def test_detection_save_all_off_rejected(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "detection",
        "data": {"track_cat": False, "track_dog": False, "track_person": False},
    })
    assert resp.status_code == 400


# ---- notifications ------------------------------------------------------

def test_telegram_test_endpoint(authed_client):
    resp = authed_client.post("/api/notifications/test", json={
        "channel": "telegram",
        "telegram_bot_token": "abc",
        "telegram_chat_id": "1",
    })
    assert resp.status_code == 200


def test_line_test_endpoint(authed_client):
    resp = authed_client.post("/api/notifications/test", json={
        "channel": "line",
        "line_channel_token": "abc",
        "line_target_id": "U123",
    })
    assert resp.status_code == 200


def test_telegram_test_missing_creds(authed_client):
    resp = authed_client.post("/api/notifications/test", json={
        "channel": "telegram", "telegram_bot_token": "", "telegram_chat_id": "",
    })
    assert resp.status_code == 400


# ---- network scan -------------------------------------------------------

def test_scan_endpoint_returns_candidates(authed_client):
    resp = authed_client.post("/api/scan", json={"cidr": "192.168.1.0/24"})
    assert resp.status_code == 200
    assert {c["ip"] for c in resp.json()["candidates"]} == {"192.168.1.100", "192.168.1.101"}


def test_scan_endpoint_rejects_bad_cidr(authed_client):
    resp = authed_client.post("/api/scan", json={"cidr": "not-a-cidr"})
    assert resp.status_code == 400


# ---- language switching -------------------------------------------------

def test_language_switch_sets_cookie(authed_client):
    resp = authed_client.post("/api/lang", json={"lang": "zh-TW"})
    assert resp.status_code == 200
    assert resp.cookies.get("pawcorder_lang") == "zh-TW"


def test_language_switch_rejects_unknown_lang(authed_client):
    resp = authed_client.post("/api/lang", json={"lang": "fr"})
    assert resp.status_code == 400


def test_language_dashboard_renders_zh_tw(authed_client):
    authed_client.post("/api/lang", json={"lang": "zh-TW"})
    # Need at least one camera so the dashboard doesn't redirect.
    authed_client.post("/api/cameras", json={"name": "cam", "ip": "1.1.1.1", "password": "x"})
    resp = authed_client.get("/")
    assert "儀表板" in resp.text


def test_language_dashboard_renders_en(authed_client):
    authed_client.post("/api/lang", json={"lang": "en"})
    authed_client.post("/api/cameras", json={"name": "cam", "ip": "1.1.1.1", "password": "x"})
    resp = authed_client.get("/")
    assert "Dashboard" in resp.text


# ---- QR code ------------------------------------------------------------

# ---- hardware page + detector save -------------------------------------

def test_hardware_page_shows_detected_platform(authed_client):
    resp = authed_client.get("/hardware")
    assert resp.status_code == 200
    # Should render some form of CPU info from our actual host.
    assert "Hardware" in resp.text or "硬體" in resp.text


def test_api_platform_returns_recommendation(authed_client):
    resp = authed_client.get("/api/platform")
    assert resp.status_code == 200
    body = resp.json()
    assert "platform" in body
    assert body["recommended_detector"] in body["valid_detectors"]


def test_save_detector_type(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "hardware", "data": {"detector_type": "openvino"},
    })
    assert resp.status_code == 200


def test_save_unknown_detector_rejected(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "hardware", "data": {"detector_type": "frobnicator"},
    })
    assert resp.status_code == 400


# ---- cloud --------------------------------------------------------------

def test_cloud_remote_crud(authed_client):
    resp = authed_client.post("/api/cloud/remote", json={
        "name": "mydrive", "backend": "drive", "fields": {"token": "{...}"},
    })
    assert resp.status_code == 200

    listing = authed_client.get("/api/cloud/remotes").json()
    assert "mydrive" in listing["remotes"]

    resp = authed_client.delete("/api/cloud/remote/mydrive")
    assert resp.status_code == 200
    assert authed_client.get("/api/cloud/remotes").json()["remotes"] == []


def test_cloud_remote_unsupported_backend(authed_client):
    resp = authed_client.post("/api/cloud/remote", json={
        "name": "x", "backend": "frobnicator", "fields": {},
    })
    assert resp.status_code == 400


def test_cloud_remote_evil_field_filtered(authed_client):
    """Verify that fields_for_backend strips unknown keys before saving."""
    from app import cloud
    authed_client.post("/api/cloud/remote", json={
        "name": "test", "backend": "b2",
        "fields": {"account": "A", "key": "K", "evil": "x"},
    })
    saved = cloud.get_remote("test")
    assert "evil" not in saved
    assert saved["account"] == "A"


def test_cloud_test_unconfigured_remote(authed_client):
    resp = authed_client.post("/api/cloud/test", json={"name": "ghost"})
    assert resp.status_code == 400


def test_cloud_save_policy(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "cloud",
        "data": {
            "cloud_enabled": True,
            "cloud_remote_path": "events",
            "cloud_upload_only_pets": True,
            "cloud_upload_min_score": "0.85",
            "cloud_retention_days": "30",
        },
    })
    assert resp.status_code == 200


def test_cloud_save_size_cap(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "cloud",
        "data": {
            "cloud_size_mode": "manual",
            "cloud_max_size_gb": "50",
        },
    })
    assert resp.status_code == 200


def test_cloud_save_adaptive_mode(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "cloud",
        "data": {
            "cloud_size_mode": "adaptive",
            "cloud_adaptive_fraction": "0.7",
        },
    })
    assert resp.status_code == 200


def test_cloud_invalid_size_mode_rejected(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "cloud", "data": {"cloud_size_mode": "frobnicate"},
    })
    assert resp.status_code == 400


def test_cloud_adaptive_fraction_out_of_range_rejected(authed_client):
    resp = authed_client.post("/api/config/save", json={
        "section": "cloud", "data": {"cloud_adaptive_fraction": "1.5"},
    })
    assert resp.status_code == 400


def test_cloud_quota_no_remote_rejected(authed_client):
    resp = authed_client.get("/api/cloud/quota")
    assert resp.status_code == 400


def test_cloud_quota_returns_data_when_configured(authed_client, monkeypatch):
    """Stub _run_rclone so the route can compute a quota response without rclone."""
    from app import cloud

    async def fake_run(*args, timeout=30):
        if "about" in args:
            return 0, '{"total":107374182400,"used":21474836480,"free":85899345920}', ""
        if "size" in args:
            return 0, '{"bytes":1073741824}', ""
        return 1, "", ""

    monkeypatch.setattr(cloud, "_run_rclone", fake_run)
    # Need a remote so the route accepts the request.
    authed_client.post("/api/cloud/remote", json={
        "name": "pawcorder", "backend": "drive", "fields": {"token": "x"},
    })
    resp = authed_client.get("/api/cloud/quota")
    assert resp.status_code == 200
    body = resp.json()
    assert body["quota_supported"] is True
    assert body["total_bytes"] == 107374182400      # 100 GB
    assert body["pawcorder_bytes"] == 1073741824    # 1 GB
    assert body["recommended_cap_bytes"] > 0


def test_manifest_served(authed_client):
    resp = authed_client.get("/static/manifest.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Pawcorder"
    assert body["short_name"] == "Pawcorder"
    assert body["display"] == "standalone"
    # New PWA hardening — id decouples from start_url, shortcuts give
    # Android long-press menu, orientation:any unlocks landscape video.
    assert body["id"]
    assert body["orientation"] == "any"
    assert isinstance(body.get("shortcuts"), list) and len(body["shortcuts"]) >= 2
    purposes = {icon.get("purpose") for icon in body["icons"]}
    assert "maskable" in purposes


def test_pwa_png_icons_served(authed_client):
    """Without these, iOS home-screen install falls back to a screenshot
    and Samsung's circular launcher mask clips the SVG corners."""
    for path in ("/static/icon-192.png", "/static/icon-512.png",
                 "/static/icon-maskable-512.png", "/static/apple-touch-icon-180.png"):
        resp = authed_client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"] == "image/png", path


def test_service_worker_served(authed_client):
    resp = authed_client.get("/static/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(("application/javascript", "text/javascript"))
    assert "serviceWorker" in resp.text or "skipWaiting" in resp.text


def test_icon_served_as_svg(authed_client):
    resp = authed_client.get("/static/icon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg")


def test_layout_has_pwa_metadata(authed_client):
    """Check the rendered HTML wires manifest + theme-color + SW registration."""
    authed_client.post("/api/cameras", json={"name": "cam", "ip": "1.1.1.1", "password": "x"})
    resp = authed_client.get("/")
    assert 'rel="manifest"' in resp.text
    assert "/static/manifest.json" in resp.text
    assert 'name="theme-color"' in resp.text
    assert "navigator.serviceWorker.register" in resp.text


def test_qrcode_returns_svg(authed_client):
    resp = authed_client.get("/api/qrcode", params={"url": "http://example.com"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.text.startswith("<?xml")


def test_qrcode_rejects_empty_url(authed_client):
    resp = authed_client.get("/api/qrcode", params={"url": ""})
    assert resp.status_code in (400, 422)


# ---- frigate restart guard ----------------------------------------------

def test_restart_blocked_when_setup_incomplete(authed_client):
    resp = authed_client.post("/api/frigate/restart", json={})
    assert resp.status_code == 400
    assert "setup not complete" in resp.json()["error"]


def test_restart_works_after_setup(authed_client):
    authed_client.post("/api/cameras", json={"name": "cam", "ip": "1.1.1.1", "password": "x"})
    resp = authed_client.post("/api/frigate/restart", json={})
    assert resp.status_code == 200


# ---- cameras x-data attribute should not be broken ---------------------

def test_cameras_page_x_data_uses_single_quotes(authed_client):
    """Regression: tojson must not break the HTML attribute."""
    authed_client.post("/api/cameras", json={"name": "cam", "ip": "1.1.1.1", "password": "x"})
    resp = authed_client.get("/cameras")
    # Either the attribute is wrapped in single quotes, OR tojson output
    # uses HTML-safe escaping. Both prevent the bug we hit before.
    assert "x-data='camerasPage(" in resp.text or "&quot;" in resp.text


# ---- /api/system/ai-tokens (Pro license + OpenAI key) ------------------

def test_ai_tokens_get_returns_presence_booleans_only(authed_client):
    """The GET endpoint must never leak the raw secret — only booleans
    (and the non-secret Ollama URL/model). The OpenAI key in particular
    must NOT appear anywhere in the response body."""
    authed_client.post("/api/system/ai-tokens", json={"openai_api_key": "sk-secret"})
    resp = authed_client.get("/api/system/ai-tokens")
    body = resp.json()
    assert body["has_openai_key"] is True
    assert body["has_pro_license"] is False
    # The actual secret must never appear in the response.
    assert "sk-secret" not in resp.text


def test_ai_tokens_post_missing_field_leaves_key_unchanged(authed_client):
    authed_client.post("/api/system/ai-tokens", json={"openai_api_key": "sk-keep"})
    # Posting only the OTHER field — OpenAI key must survive.
    authed_client.post("/api/system/ai-tokens", json={"pawcorder_pro_license_key": "pro_x"})
    body = authed_client.get("/api/system/ai-tokens").json()
    assert body["has_openai_key"] is True
    assert body["has_pro_license"] is True


def test_ai_tokens_post_explicit_empty_clears_key(authed_client):
    """Critical: a user rotating a leaked key must be able to wipe it."""
    authed_client.post("/api/system/ai-tokens", json={"openai_api_key": "sk-old"})
    assert authed_client.get("/api/system/ai-tokens").json()["has_openai_key"] is True
    authed_client.post("/api/system/ai-tokens", json={"openai_api_key": ""})
    assert authed_client.get("/api/system/ai-tokens").json()["has_openai_key"] is False


@pytest.mark.parametrize("bad_value", [
    "key\nwith-newline",        # would break the .env line
    "key\rwith-cr",
    "key\x00with-nul",
    'key"with-doublequote',     # would close the env value
    "key'with-singlequote",
])
def test_ai_tokens_post_rejects_corrupting_chars(authed_client, bad_value: str):
    resp = authed_client.post("/api/system/ai-tokens", json={"openai_api_key": bad_value})
    assert resp.status_code == 400
    assert "forbidden characters" in resp.json()["error"]


def test_ai_tokens_post_rejects_oversize_value(authed_client):
    huge = "sk-" + ("x" * 300)
    resp = authed_client.post("/api/system/ai-tokens", json={"openai_api_key": huge})
    assert resp.status_code == 400
    assert "256-character" in resp.json()["error"]
