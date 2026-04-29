"""Tests for the schema migration runner."""
from __future__ import annotations

import yaml


def test_migrate_unversioned_cameras_yml(data_dir):
    """An old cameras.yml with no schema_version: run all migrations."""
    from app import migrations

    cams_path = data_dir / "config" / "cameras.yml"
    cams_path.write_text(
        "cameras:\n"
        "  - name: living_room\n"
        "    ip: 192.168.1.100\n"
        "    user: admin\n"
        "    password: x\n"
        "    rtsp_port: 554\n"
        "    onvif_port: 8000\n"
        "    detect_width: 640\n"
        "    detect_height: 480\n"
        "    enabled: true\n",
    )

    result = migrations.migrate_file("config/cameras.yml")
    assert result.error == ""
    assert result.from_version == 0
    assert result.to_version == 2
    assert 1 in result.applied and 2 in result.applied

    out = yaml.safe_load(cams_path.read_text())
    assert out["schema_version"] == 2
    cam = out["cameras"][0]
    assert cam["zones"] == []
    assert cam["privacy_masks"] == []
    assert cam["audio_detection"] is False
    assert cam["ptz_presets"] == []
    # Existing fields preserved.
    assert cam["name"] == "living_room"
    assert cam["password"] == "x"


def test_migrate_idempotent(data_dir):
    """Running twice is a no-op on the second pass."""
    from app import migrations

    cams_path = data_dir / "config" / "cameras.yml"
    cams_path.write_text("cameras: []\n")

    first = migrations.migrate_file("config/cameras.yml")
    second = migrations.migrate_file("config/cameras.yml")
    assert first.applied == [1, 2]
    assert second.applied == []  # already at v2


def test_migrate_missing_file_no_error(data_dir):
    from app import migrations
    result = migrations.migrate_file("config/cameras.yml")
    assert result.error == ""
    assert result.applied == []


def test_migrate_pets_yml(data_dir):
    from app import migrations
    (data_dir / "config" / "pets.yml").write_text("pets: []\n")
    result = migrations.migrate_file("config/pets.yml")
    assert result.to_version == 1
    assert result.applied == [1]


def test_run_all_returns_one_per_file(data_dir):
    from app import migrations
    results = migrations.run_all()
    files = {r.file for r in results}
    assert files == set(migrations.MIGRATIONS.keys())


def test_current_versions_matches_table(data_dir):
    from app import migrations
    versions = migrations.current_versions()
    assert versions["config/cameras.yml"] == 2
    assert versions["config/pets.yml"] == 1
