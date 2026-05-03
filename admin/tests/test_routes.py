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


def test_mobile_page_uses_host_ports_from_env(authed_client, monkeypatch):
    # When install.sh has to relocate ports (e.g. macOS AirPlay holding
    # 5000), it writes the picked values into the admin container's env.
    # The /mobile page must use those, not the hardcoded defaults — the
    # QR codes printed to the user's phone need the *actual* URL.
    monkeypatch.setenv("ADMIN_HOST_PORT", "8090")
    monkeypatch.setenv("FRIGATE_HOST_PORT", "5050")
    resp = authed_client.get("/mobile")
    assert resp.status_code == 200
    assert ":8090" in resp.text
    assert ":5050" in resp.text
    assert ":8080" not in resp.text  # no leftover hardcoded admin port
    assert ":5000" not in resp.text  # no leftover hardcoded frigate port


def test_mobile_page_defaults_to_standard_ports(authed_client, monkeypatch):
    # When ADMIN_HOST_PORT / FRIGATE_HOST_PORT aren't set (e.g. someone
    # running the admin container outside install.sh) the page still
    # renders with sensible defaults so it doesn't break Linux installs.
    monkeypatch.delenv("ADMIN_HOST_PORT", raising=False)
    monkeypatch.delenv("FRIGATE_HOST_PORT", raising=False)
    resp = authed_client.get("/mobile")
    assert resp.status_code == 200
    assert ":8080" in resp.text
    assert ":5000" in resp.text


def test_setup_wizard_offers_wireless_onboarding_branch(authed_client):
    # The cameras step (step 1 in the wizard) must let a brand-new user
    # whose camera is only plugged into power get to /onboarding/wireless
    # without dead-ending on a 0-result subnet scan.
    resp = authed_client.get("/setup")
    assert resp.status_code == 200
    assert "/onboarding/wireless" in resp.text
    # The connection-mode picker must be wired in — the i18n key for
    # "only plugged into power" appears as rendered copy when the page
    # is in Chinese (default test fixture is en, but the picker title
    # is always present).
    assert "powered_only" in resp.text


def test_cameras_page_links_to_wireless_onboarding(authed_client):
    # The /cameras page is the post-setup landing for adding more
    # cameras. Same wireless-only-camera escape hatch needs to be
    # reachable here, otherwise a user adding a second wireless camera
    # six months later hits the same dead end as the wizard used to.
    resp = authed_client.get("/cameras")
    assert resp.status_code == 200
    assert "/onboarding/wireless" in resp.text


def test_wireless_visible_ssids_returns_ranked_list(authed_client, tmp_path, monkeypatch):
    # Pretend the host helper file at /data/.wifi_scan.json exists with a
    # mix of camera-shaped and home-router SSIDs. The endpoint should
    # return ALL of them (no fingerprint gate), with camera-shaped SSIDs
    # ranked first so a no-name dropship cam is findable when the
    # fingerprint-only scan returns nothing.
    snap = tmp_path / "wifi_scan.json"
    import json
    snap.write_text(json.dumps({
        "schema": 1,
        "generated_at": int(__import__("time").time()),
        "platform": "macos", "tool": "system_profiler",
        "networks": [
            {"ssid": "MyHomeWifi",       "bssid": "", "signal_dbm": -45, "channel": 6},
            {"ssid": "ANRAN-IPC-A1B2",   "bssid": "", "signal_dbm": -68, "channel": 1},
            {"ssid": "neighbor-5G",      "bssid": "", "signal_dbm": -75, "channel": 36},
            {"ssid": "MV-AABBCC",        "bssid": "", "signal_dbm": -60, "channel": 11},
        ],
        "error": None,
    }))
    monkeypatch.setenv("WIFI_SCAN_FILE", str(snap))
    import importlib
    import app.provisioning.softap_scanner as ss
    importlib.reload(ss)

    resp = authed_client.get("/api/onboarding/wireless/visible-ssids")
    assert resp.status_code == 200
    body = resp.json()
    nets = body["networks"]
    assert len(nets) == 4
    # camera-shaped SSIDs come first
    assert nets[0]["ssid"] in {"ANRAN-IPC-A1B2", "MV-AABBCC"}
    assert nets[1]["ssid"] in {"ANRAN-IPC-A1B2", "MV-AABBCC"}
    # And both are flagged as looking like cameras
    assert all(n["looks_like_camera"] for n in nets[:2])
    # The home / neighbour SSIDs aren't flagged
    home = next(n for n in nets if n["ssid"] == "MyHomeWifi")
    assert home["looks_like_camera"] is False


def test_wireless_visible_ble_returns_ranked_list(authed_client, tmp_path, monkeypatch):
    # Same shape as visible-ssids but for BLE: feed the host helper file
    # with a mix of Apple noise, an unnamed non-Apple device with a
    # service UUID (the OTS-0x1827 case our beeping no-name camera
    # actually exhibits), and verify the camera-shaped one ranks first.
    snap = tmp_path / "ble_scan.json"
    import json
    snap.write_text(json.dumps({
        "schema": 1,
        "generated_at": int(__import__("time").time()),
        "platform": "macos", "tool": "bleak",
        "devices": [
            {"address": "AAA", "rssi": -50, "name": "iPhoneOfXup", "service_uuids": [], "manufacturer_ids": [76]},
            {"address": "BBB", "rssi": -60, "name": "", "service_uuids": ["00001827-0000-1000-8000-00805f9b34fb"], "manufacturer_ids": []},
            {"address": "CCC", "rssi": -90, "name": "", "service_uuids": [], "manufacturer_ids": [76]},
        ],
        "error": None,
    }))
    monkeypatch.setenv("BLE_SCAN_FILE", str(snap))
    import importlib
    import app.provisioning.ble_scanner as bs
    importlib.reload(bs)

    resp = authed_client.get("/api/onboarding/wireless/visible-ble")
    assert resp.status_code == 200
    body = resp.json()
    devs = body["devices"]
    assert len(devs) == 3
    # The non-Apple OTS device wins the sort.
    assert devs[0]["address"] == "BBB"
    assert devs[0]["looks_like_camera"] is True
    assert devs[0]["is_apple"] is False
    # The named iPhone is Apple but not flagged as a camera.
    iphone = next(d for d in devs if d["address"] == "AAA")
    assert iphone["is_apple"] is True
    assert iphone["looks_like_camera"] is False


def test_wireless_visible_ble_empty_when_helper_missing(authed_client, tmp_path, monkeypatch):
    monkeypatch.setenv("BLE_SCAN_FILE", str(tmp_path / "missing.json"))
    import importlib
    import app.provisioning.ble_scanner as bs
    importlib.reload(bs)
    resp = authed_client.get("/api/onboarding/wireless/visible-ble")
    assert resp.status_code == 200
    body = resp.json()
    assert body["devices"] == []
    assert body["error"] == "stale"


def test_ble_host_helper_promotes_fingerprinted_devices(tmp_path, monkeypatch):
    # When the host snapshot contains a Tapo BLE advertisement (service
    # UUID 0xFFF0), scan_once should return a DiscoveredDevice tagged
    # with the Tapo fingerprint — same as the in-container path used to
    # do, just sourced from the snapshot file.
    snap = tmp_path / "ble_scan.json"
    import json
    snap.write_text(json.dumps({
        "schema": 1, "generated_at": int(__import__("time").time()),
        "platform": "macos", "tool": "bleak",
        "devices": [
            {"address": "AA:BB:CC:DD:EE:FF", "rssi": -55, "name": "Tapo_C200_X1",
             "service_uuids": ["0000fff0-0000-1000-8000-00805f9b34fb"],
             "manufacturer_ids": []},
        ],
        "error": None,
    }))
    monkeypatch.setenv("BLE_SCAN_FILE", str(snap))
    import importlib, asyncio
    import app.provisioning.ble_scanner as bs
    importlib.reload(bs)
    devices = asyncio.run(bs.scan_once())
    assert len(devices) == 1
    assert devices[0].vendor == "tapo"


def test_wireless_visible_ssids_empty_when_helper_missing(authed_client, tmp_path, monkeypatch):
    monkeypatch.setenv("WIFI_SCAN_FILE", str(tmp_path / "definitely-missing.json"))
    import importlib
    import app.provisioning.softap_scanner as ss
    importlib.reload(ss)
    resp = authed_client.get("/api/onboarding/wireless/visible-ssids")
    assert resp.status_code == 200
    body = resp.json()
    assert body["networks"] == []
    assert body["error"] == "stale"


def test_tuya_smartlife_softap_ssid_is_fingerprinted():
    # The "SmartLife-0000" SSID is the white-label default a fresh
    # Tuya/Smart Life camera broadcasts before it's been claimed by
    # any account — the most common no-name camera the user will
    # encounter on Shopee / Amazon / AliExpress. Was previously
    # invisible because no fingerprint covered the prefix.
    from app.provisioning.fingerprints import match_softap
    for ssid in ("SmartLife-0000", "SmartLife_AABBCC", "SL-A1B2C3D4",
                 "Tuya_AP-1234", "Tuya-CamX"):
        fp = match_softap(ssid)
        assert fp is not None, f"{ssid!r} should match Tuya/SmartLife fingerprint"
        assert "tuya" in fp.id or "smart" in fp.label.lower(), \
            f"{ssid!r} matched unexpected fingerprint {fp.id}"


def test_every_vendor_handoff_fingerprint_carries_verified_app_links():
    # All Class-B (cloud-locked) fingerprints have to point at a real
    # App Store / Play Store listing, otherwise the wireless-onboarding
    # vendor-handoff card opens a 404 in the user's browser. We can't
    # actually ping the stores from CI (network egress + rate limits),
    # so we shape-check: HTTPS apple/google-play URL with a non-empty
    # ID/package segment. The IDs themselves were verified by hand on
    # 2026-05-03 (see commit; future drift needs human re-verification —
    # noted in HUMAN_WORK.md).
    import re as _re
    from app.provisioning.fingerprints import FINGERPRINTS
    apple_re = _re.compile(r"^https://apps\.apple\.com/(?:[a-z]{2}/)?app/(?:[^/]+/)?id\d{6,}$")
    play_re = _re.compile(r"^https://play\.google\.com/store/apps/details\?id=[a-z0-9_.]+$",
                           _re.IGNORECASE)
    seen = 0
    for fp in FINGERPRINTS:
        if fp.capability != "vendor":
            continue
        ios = fp.metadata.get("vendor_app_ios", "")
        android = fp.metadata.get("vendor_app_android", "")
        # Either both must be provided, or neither (so the UI hides
        # the App Store / Play row entirely rather than showing one
        # broken half).
        assert bool(ios) == bool(android), \
            f"{fp.id}: must specify both iOS and Android or neither"
        if ios:
            assert apple_re.match(ios), f"{fp.id}: bad iOS link {ios!r}"
            assert play_re.match(android), f"{fp.id}: bad Android link {android!r}"
            seen += 1
    # Sanity: at least the original vendor list has handoff links.
    assert seen >= 7, f"only {seen} vendor fingerprints carry app links"


def test_discovered_device_to_dict_forwards_vendor_app_links_only():
    # DiscoveredDevice.extra carries both UI-safe keys (vendor_app_*)
    # and noisy keys (raw manufacturer_data, service_uuids). The
    # to_dict allow-list must forward the URLs the handoff card
    # needs and drop the protocol-internal stuff.
    from app.provisioning.base import DiscoveredDevice
    d = DiscoveredDevice(
        id="x", transport="softap", vendor="other", capability="vendor",
        extra={
            "vendor_app_ios": "https://apps.apple.com/app/idABC",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=Y",
            "softap_ip": "192.168.4.1",
            "manufacturer_data": b"\x00\x01\x02",
            "service_uuids": ["unrelated-uuid"],
            "internal_token": "should-not-leak",
        },
    )
    body = d.to_dict()
    assert body["extra"]["vendor_app_ios"].endswith("idABC")
    assert "play.google" in body["extra"]["vendor_app_android"]
    assert body["extra"]["softap_ip"] == "192.168.4.1"
    # Noisy / internal keys must not survive serialisation.
    assert "manufacturer_data" not in body["extra"]
    assert "service_uuids" not in body["extra"]
    assert "internal_token" not in body["extra"]


def test_master_key_treats_fail_keyring_module_as_unavailable(monkeypatch):
    # Regression: ``keyring.backends.fail.Keyring`` returns a class
    # whose ``__name__`` is just ``Keyring`` — the old "fail" / "null"
    # substring check on the bare class name missed it, the master_key
    # module picked the no-op backend, and every wifi_creds.save() in
    # the admin container crashed with NoKeyringError. With the fix in
    # place we look at __module__ too and fall through to file backend.
    import importlib
    import app.master_key as mk
    importlib.reload(mk)

    class _FailingKeyring:
        pass
    _FailingKeyring.__module__ = "keyring.backends.fail"
    _FailingKeyring.__qualname__ = "Keyring"
    _FailingKeyring.__name__ = "Keyring"

    class _FakeKeyring:
        def get_keyring(self):
            return _FailingKeyring()

    monkeypatch.setitem(__import__("sys").modules, "keyring", _FakeKeyring())
    monkeypatch.setitem(__import__("sys").modules, "keyring.errors",
                        type("M", (), {"KeyringError": Exception}))
    assert mk._keyring_available() is False


def _stub_keyring_unavailable(mk_module, monkeypatch):
    """Replace the _BACKENDS tuple so keyring + tpm report unavailable.

    ``_BACKENDS`` captures function references at module-import time, so
    monkeypatching the module-level ``_keyring_available`` doesn't
    propagate into the tuple — we have to swap the whole tuple entries.
    """
    new_specs = []
    for spec in mk_module._BACKENDS:
        if spec.name in ("keyring", "tpm"):
            new_specs.append(mk_module._BackendSpec(
                name=spec.name, detail=spec.detail,
                available=lambda: False, get_or_create=spec.get_or_create,
            ))
        else:
            new_specs.append(spec)
    monkeypatch.setattr(mk_module, "_BACKENDS", tuple(new_specs))


def test_master_key_self_heals_when_persisted_backend_unavailable_and_no_data(
    tmp_path, monkeypatch,
):
    # Simulates the exact post-bug state on a Mac admin: an earlier
    # version picked "keyring" but no save() ever succeeded so no
    # ciphertexts exist. The fix must silently re-pick to "file"
    # rather than raise, otherwise the user is stuck.
    import importlib
    monkeypatch.setenv("PAWCORDER_DATA_DIR", str(tmp_path))
    import app.master_key as mk
    importlib.reload(mk)
    (tmp_path / "master_key.meta.json").write_text(
        '{"backend":"keyring","detail":"OS keyring (Keychain / DPAPI / Secret Service)"}'
    )
    _stub_keyring_unavailable(mk, monkeypatch)
    info = mk.get_master_key()
    assert info.backend == "file"
    # And the meta gets rewritten so subsequent calls go straight there.
    import json as _j
    new_meta = _j.loads((tmp_path / "master_key.meta.json").read_text())
    assert new_meta["backend"] == "file"


def test_master_key_refuses_to_self_heal_when_data_exists(tmp_path, monkeypatch):
    # Inverse: if cipher blobs exist, swapping backends would break
    # them, so we MUST raise and let the human investigate.
    import importlib
    monkeypatch.setenv("PAWCORDER_DATA_DIR", str(tmp_path))
    import app.master_key as mk
    import app.wifi_creds as wc
    importlib.reload(mk)
    importlib.reload(wc)
    (tmp_path / "master_key.meta.json").write_text(
        '{"backend":"keyring","detail":"..."}'
    )
    wc.WIFI_DIR.mkdir(parents=True, exist_ok=True)
    (wc.WIFI_DIR / "fake.json").write_text("{}")
    _stub_keyring_unavailable(mk, monkeypatch)
    import pytest as _pt
    with _pt.raises(RuntimeError, match="no longer available"):
        mk.get_master_key()


def test_provision_endpoint_does_not_crash_when_keyring_unavailable(
    authed_client, monkeypatch,
):
    # The /provision endpoint would 500 with "internal error" when
    # remember=True and the keyring backend was the no-op (e.g. inside
    # a Docker container with no Secret Service). The save is best-
    # effort; the provision call must still proceed.
    import app.wifi_creds as wifi_creds

    def _explode(*_a, **_k):
        raise wifi_creds.WifiCredsError("(simulated) no keyring")

    monkeypatch.setattr(wifi_creds, "save", _explode)
    resp = authed_client.post("/api/onboarding/wireless/provision", json={
        "device": {
            "id": "fake", "transport": "softap", "vendor": "other",
            "label": "Fake Cam", "capability": "vendor",
            "fingerprint_id": "tuya-softap",
        },
        "ssid": "lin999sir",
        "psk": "supersecret",
        "auth": "wpa2-psk",
        "remember": True,
    })
    # We should get a 200 with an NDJSON body — the orchestrator's
    # vendor-handoff path takes over even though save() exploded.
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}"
    assert "selected" in resp.text  # first SSE event is "selected"


def test_smartlife_ssid_ranks_as_camera_in_visible_ssids(authed_client, tmp_path, monkeypatch):
    # End-to-end: SmartLife-0000 has to surface AND get the
    # looks_like_camera badge so the user spots it among home routers.
    snap = tmp_path / "wifi_scan.json"
    import json
    snap.write_text(json.dumps({
        "schema": 1, "generated_at": int(__import__("time").time()),
        "platform": "macos", "tool": "system_profiler",
        "networks": [
            {"ssid": "lin999sir",        "bssid": "", "signal_dbm": -45, "channel": 6},
            {"ssid": "SmartLife-0000",   "bssid": "", "signal_dbm": -65, "channel": 1},
            {"ssid": "BB29N1F-5G",       "bssid": "", "signal_dbm": -70, "channel": 149},
        ],
        "error": None,
    }))
    monkeypatch.setenv("WIFI_SCAN_FILE", str(snap))
    import importlib
    import app.provisioning.softap_scanner as ss
    importlib.reload(ss)
    resp = authed_client.get("/api/onboarding/wireless/visible-ssids")
    body = resp.json()
    sl = next((n for n in body["networks"] if n["ssid"] == "SmartLife-0000"), None)
    assert sl is not None
    assert sl["looks_like_camera"] is True
    # And it ranks before the home routers.
    assert body["networks"][0]["ssid"] == "SmartLife-0000"


def test_no_name_camera_softap_ssid_is_fingerprinted():
    # Worst-case-user safety net: dropship cameras with iCSee / CamHi /
    # V380 Pro / MIPC SSIDs need to show up in the wireless onboarding
    # device list, otherwise the user sees "no cameras found" and dead-
    # ends. These prefixes were chosen from the most common no-name
    # camera apps on Shopee / Lazada / Amazon.
    from app.provisioning.fingerprints import match_softap
    for ssid in ("iCSee_AABBCC", "MV+1234567", "V380-AB12CD", "MIPC_xyz",
                 "JXLCAM-ABCDEF", "ICAM-12345", "ATOM_AABBCC", "EYE4-99887766",
                 "IPC365_AB12", "IP-CAMERA-XX", "IPCAM_aabbcc"):
        fp = match_softap(ssid)
        assert fp is not None, f"{ssid!r} should match no-name fingerprint"
        assert fp.id in {"no-name-camera-softap", "foscam-softap", "espressif-softap"}, \
            f"{ssid!r} matched unexpected fingerprint {fp.id}"


def test_wireless_status_reports_scan_reason(authed_client, tmp_path, monkeypatch):
    # The status route surfaces softap_scan_reason so the page can swap
    # banner copy ("no Wi-Fi card" vs "scanner not yet running" vs the
    # generic fallback). Without a fresh helper file and no in-container
    # scan tools, the reason should be "stale" or "no_scan_tool".
    monkeypatch.setenv("WIFI_SCAN_FILE", str(tmp_path / "missing.json"))
    import importlib
    import app.provisioning.softap_scanner as ss
    importlib.reload(ss)
    resp = authed_client.get("/api/onboarding/wireless/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "softap_scan" in body["capabilities"]
    assert "softap_scan_reason" in body["capabilities"]
    # We don't pin the exact reason — it depends on whether the test
    # host has nmcli/iw/airport on PATH — but it has to be a string.
    assert isinstance(body["capabilities"]["softap_scan_reason"], str)


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
