"""Tests for the vendor fingerprint database and matchers."""
from __future__ import annotations

import pytest


def test_match_ble_by_service_uuid(data_dir):
    from app.provisioning import fingerprints

    matter_uuid = "0000fff6-0000-1000-8000-00805f9b34fb"
    fp = fingerprints.match_ble(
        advertised_uuids=[matter_uuid],
        local_name="",
        mac="aa:bb:cc:dd:ee:ff",
        manufacturer_ids=[],
    )
    assert fp is not None
    assert fp.id == "matter-generic"


def test_match_ble_homekit_uuid(data_dir):
    from app.provisioning import fingerprints

    homekit_uuid = "0000fe5c-0000-1000-8000-00805f9b34fb"
    fp = fingerprints.match_ble(
        advertised_uuids=[homekit_uuid],
        local_name="Eve Cam",
        mac="aa:bb:cc:dd:ee:ff",
        manufacturer_ids=[],
    )
    assert fp is not None
    assert fp.id == "homekit-generic"


def test_match_ble_by_local_name_pattern(data_dir):
    from app.provisioning import fingerprints

    fp = fingerprints.match_ble(
        advertised_uuids=[],
        local_name="Tapo_C200_AB12",
        mac="9c:53:22:00:00:01",
        manufacturer_ids=[],
    )
    assert fp is not None
    assert fp.id == "tapo-ble"
    assert fp.capability == "vendor"


def test_match_ble_tuya_by_manufacturer_id(data_dir):
    from app.provisioning import fingerprints

    fp = fingerprints.match_ble(
        advertised_uuids=[],
        local_name="",
        mac="aa:bb:cc:dd:ee:01",
        manufacturer_ids=[0x07D0],
    )
    assert fp is not None
    assert fp.id == "tuya-ble"


def test_match_ble_returns_none_for_unrelated_device(data_dir):
    from app.provisioning import fingerprints

    fp = fingerprints.match_ble(
        advertised_uuids=["0000180a-0000-1000-8000-00805f9b34fb"],  # generic Device Info service
        local_name="MyHeartrateMonitor",
        mac="aa:bb:cc:00:00:01",
        manufacturer_ids=[],
    )
    assert fp is None


def test_match_softap_foscam(data_dir):
    from app.provisioning import fingerprints

    assert fingerprints.match_softap("IPC-AB12CD34").id == "foscam-softap"
    assert fingerprints.match_softap("foscam_room").id == "foscam-softap"


def test_match_softap_dahua(data_dir):
    from app.provisioning import fingerprints

    assert fingerprints.match_softap("DH-IPC-HFW12").id == "dahua-softap"


def test_match_softap_imou_is_detection_only(data_dir):
    from app.provisioning import fingerprints

    fp = fingerprints.match_softap("Imou_LCxxxx")
    assert fp is not None
    assert fp.id == "imou-softap"
    assert fp.capability == "vendor"


def test_match_softap_returns_none_for_home_wifi(data_dir):
    from app.provisioning import fingerprints

    assert fingerprints.match_softap("MyHomeWifi") is None


def test_by_id_and_for_transport_lookups(data_dir):
    from app.provisioning import fingerprints

    fp = fingerprints.by_id("foscam-softap")
    assert fp is not None
    assert fp.vendor == "foscam"
    softap_fps = fingerprints.for_transport("softap")
    assert any(f.id == "foscam-softap" for f in softap_fps)
    ble_fps = fingerprints.for_transport("ble")
    assert any(f.id == "matter-generic" for f in ble_fps)
