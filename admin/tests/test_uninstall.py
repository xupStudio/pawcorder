"""Tests for the uninstall module + routes."""
from __future__ import annotations

import pytest


def _seed_app_data(data_dir):
    """Drop a few representative pawcorder-managed files into a fresh
    data dir so the inventory has something to count."""
    (data_dir / "config" / "cameras.yml").write_text("cameras: []\n")
    (data_dir / "config" / "pets.yml").write_text("pets: []\n")
    (data_dir / "config" / "privacy.json").write_text("{}")
    (data_dir / "config" / "sightings.ndjson").write_text("")
    (data_dir / "pets").mkdir(exist_ok=True)
    (data_dir / "pets" / "mochi").mkdir(exist_ok=True)
    (data_dir / "pets" / "mochi" / "p.jpg").write_text("FAKEJPEG")
    (data_dir / "models").mkdir(exist_ok=True)
    (data_dir / "models" / "embedding_model.onnx").write_text("FAKE_ONNX_BLOB")


# ---- inventory ---------------------------------------------------------

def test_inventory_lists_app_data(data_dir):
    from app import uninstall

    _seed_app_data(data_dir)
    inv = uninstall.take_inventory()

    paths = {f.path for f in inv.config_files}
    assert any("cameras.yml" in p for p in paths)
    assert any("pets.yml" in p for p in paths)
    assert any("privacy.json" in p for p in paths)

    dirs = {f.path for f in inv.config_dirs}
    assert any(p.endswith("/pets") for p in dirs)
    assert any(p.endswith("/models") for p in dirs)


def test_inventory_reports_sizes(data_dir):
    from app import uninstall

    _seed_app_data(data_dir)
    inv = uninstall.take_inventory()
    assert inv.total_app_data_bytes() > 0


def test_inventory_marks_missing_paths_as_not_present(data_dir):
    """A fresh data dir with no pets.yml etc. → entries exist but
    exists=False so the UI can show a dimmed row."""
    from app import uninstall
    inv = uninstall.take_inventory()
    by_label = {f.label_key: f for f in inv.config_files}
    # Test fixture only writes .env; pets.yml should be missing.
    assert by_label["UNINSTALL_PATH_PETS"].exists is False
    assert by_label["UNINSTALL_PATH_PETS"].size_bytes == 0


def test_inventory_includes_recordings_path(data_dir):
    """STORAGE_PATH from .env must show up as the recordings entry."""
    from app import uninstall
    inv = uninstall.take_inventory()
    assert inv.storage_path is not None
    assert inv.storage_path.label_key == "UNINSTALL_PATH_RECORDINGS"


def test_inventory_to_dict_serialises(data_dir):
    """Routes return JSON — make sure every nested dataclass converts
    cleanly with no numpy/datetime/Path leaks."""
    import json
    from app import uninstall

    _seed_app_data(data_dir)
    payload = uninstall.take_inventory().to_dict()
    # Must round-trip through json without errors.
    assert json.loads(json.dumps(payload))


# ---- soft reset --------------------------------------------------------

def test_reset_app_data_removes_files_and_dirs(data_dir):
    from app import uninstall

    _seed_app_data(data_dir)
    result = uninstall.reset_app_data()

    assert result.ok
    assert not (data_dir / "config" / "cameras.yml").exists()
    assert not (data_dir / "config" / "pets.yml").exists()
    assert not (data_dir / "pets").exists()
    assert not (data_dir / "models").exists()


def test_reset_app_data_preserves_admin_password(data_dir):
    """The whole point of the soft reset — user keeps their login. If
    we wiped ADMIN_PASSWORD they'd be locked out by their own click."""
    from app import config_store, uninstall

    _seed_app_data(data_dir)
    cfg = config_store.load_config()
    cfg.admin_password = "preserve-me"
    cfg.admin_session_secret = "also-keep-me"
    cfg.tailscale_hostname = "should-be-cleared"
    cfg.telegram_enabled = True
    config_store.save_config(cfg)

    uninstall.reset_app_data()

    after = config_store.load_config()
    assert after.admin_password == "preserve-me"
    assert after.admin_session_secret == "also-keep-me"
    # Other keys should be back to defaults.
    assert after.tailscale_hostname == ""
    assert after.telegram_enabled is False


def test_reset_clears_credentials(data_dir):
    """API keys, VAPID keys, backup encryption password — all
    credentials/secrets must be wiped by reset, otherwise they
    survive a 'fresh start' and create real security exposure."""
    from app import uninstall

    # Seed each kind of credential file.
    cfg_dir = data_dir / "config"
    (cfg_dir / "api_keys.json").write_text('{"keys":[{"sha256_hex":"x","name":"y","key_id":"z"}]}')
    (cfg_dir / "webpush_vapid.json").write_text('{"private_key_pem":"-----PRIVATE-----"}')
    (cfg_dir / "webpush_subs.json").write_text('{"subs":[]}')
    (cfg_dir / "backup_schedule.json").write_text('{"encryption_password":"hunter2"}')
    (cfg_dir / "energy_mode.json").write_text('{"enabled":true}')

    uninstall.reset_app_data()

    for f in ("api_keys.json", "webpush_vapid.json", "webpush_subs.json",
              "backup_schedule.json", "energy_mode.json"):
        assert not (cfg_dir / f).exists(), f"{f} survived reset"


def test_inventory_includes_all_credential_files(data_dir):
    """Every credential file we now expect to clean up should be
    listed in the inventory so the UI shows users what's at risk."""
    from app import uninstall
    inv = uninstall.take_inventory()
    paths = {f.path for f in inv.config_files}
    expected = {
        "api_keys.json", "webpush_vapid.json", "webpush_subs.json",
        "backup_schedule.json", "energy_mode.json",
    }
    for name in expected:
        assert any(name in p for p in paths), f"{name} missing from inventory"


def test_reset_app_data_does_not_touch_storage_path(data_dir, monkeypatch):
    """Soft reset must NOT touch STORAGE_PATH — recordings can be huge
    and irreplaceable."""
    from app import uninstall

    storage = data_dir / "fake_recordings"
    storage.mkdir()
    (storage / "kitchen").mkdir()
    (storage / "kitchen" / "evt1.mp4").write_text("video bytes")

    # Point the config at it so reset would pick it up if it tried.
    from app import config_store
    cfg = config_store.load_config()
    cfg.storage_path = str(storage)
    config_store.save_config(cfg)

    uninstall.reset_app_data()

    assert (storage / "kitchen" / "evt1.mp4").exists()


def test_reset_idempotent(data_dir):
    """Second call shouldn't crash even though first call removed everything."""
    from app import uninstall
    _seed_app_data(data_dir)
    uninstall.reset_app_data()
    result2 = uninstall.reset_app_data()
    assert result2.ok


# ---- command generator -------------------------------------------------

def test_uninstall_command_soft_keeps_project_dir(data_dir):
    from app import uninstall
    cmd = uninstall.uninstall_command("soft")
    assert "compose down" in cmd
    assert "rm -rf" not in cmd  # soft doesn't delete the project dir


def test_uninstall_command_full_removes_project_dir(data_dir):
    from app import uninstall
    cmd = uninstall.uninstall_command("full", project_dir="/opt/pawcorder")
    assert "compose down -v" in cmd
    assert "rm -rf /opt/pawcorder" in cmd
    # But it must NOT touch the recordings path.
    assert "/mnt/pawcorder" not in cmd or cmd.count("/mnt/pawcorder") == 0


def test_uninstall_command_nuke_includes_recordings(data_dir):
    """Nuke must call out the recordings path explicitly so the user
    sees what's getting nuked when they preview the command."""
    from app import config_store, uninstall

    cfg = config_store.load_config()
    cfg.storage_path = "/mnt/pawcorder-recordings"
    config_store.save_config(cfg)

    cmd = uninstall.uninstall_command("nuke")
    assert "/mnt/pawcorder-recordings" in cmd
    assert "rm -rf" in cmd


def test_uninstall_command_unknown_level_raises(data_dir):
    from app import uninstall
    with pytest.raises(ValueError):
        uninstall.uninstall_command("frobnicate")


def test_humanize_bytes(data_dir):
    from app.uninstall import humanize_bytes
    assert humanize_bytes(0) == "0 B"
    assert humanize_bytes(2048).endswith("KB")
    assert humanize_bytes(2 * 1024 * 1024).endswith("MB")
    assert humanize_bytes(2 * 1024 * 1024 * 1024).endswith("GB")


# ---- routes ------------------------------------------------------------

def test_inventory_route_requires_auth(app_client):
    resp = app_client.get("/api/uninstall/inventory")
    assert resp.status_code == 401


def test_inventory_route_returns_payload(authed_client):
    resp = authed_client.get("/api/uninstall/inventory")
    assert resp.status_code == 200
    body = resp.json()
    assert "config_files" in body
    assert "containers" in body
    assert "total_app_data_bytes" in body


def test_reset_route_actually_resets(authed_client, data_dir):
    """End-to-end: cameras.yml gets cleared by the route call."""
    _seed_app_data(data_dir)
    assert (data_dir / "config" / "cameras.yml").exists()
    resp = authed_client.post("/api/uninstall/reset", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert not (data_dir / "config" / "cameras.yml").exists()


def test_command_route_validates_level(authed_client):
    resp = authed_client.get("/api/uninstall/command?level=hax")
    assert resp.status_code == 400


def test_command_route_returns_for_each_valid_level(authed_client):
    for level in ("soft", "full", "nuke"):
        resp = authed_client.get(f"/api/uninstall/command?level={level}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["level"] == level
        assert "command" in body
        assert len(body["command"]) > 30  # something substantive


def test_reset_route_requires_csrf(authed_client):
    """The reset route is highly destructive — it must not be reachable
    without the CSRF header."""
    resp = authed_client.post(
        "/api/uninstall/reset",
        headers={"X-Requested-With": ""},
        json={},
    )
    assert resp.status_code == 403
