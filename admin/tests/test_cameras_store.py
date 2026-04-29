"""Tests for the cameras.yml CRUD layer."""
from __future__ import annotations

import pytest


def test_load_empty(data_dir):
    from app.cameras_store import CameraStore
    store = CameraStore()
    assert store.load() == []


def test_create_and_load(data_dir):
    from app.cameras_store import Camera, CameraStore
    store = CameraStore()
    store.create(Camera(name="living_room", ip="192.168.1.100", password="x"))
    store.create(Camera(name="kitchen",     ip="192.168.1.101", password="y"))
    cams = store.load()
    assert [c.name for c in cams] == ["living_room", "kitchen"]


def test_save_atomic_kill_preserves_old_data(data_dir, monkeypatch):
    """Crash between write and os.replace must leave old cameras.yml intact.

    Without atomic save, a non-atomic write_text would truncate the file
    first, then re-fill it — a kill between those steps leaves a half-
    written YAML that load() falls back to [] on, silently wiping the
    user's camera list.
    """
    from app import cameras_store
    from app.cameras_store import Camera, CameraStore

    store = CameraStore()
    store.create(Camera(name="original", ip="1.1.1.1", password="x"))

    def _boom(*_args, **_kw):
        raise OSError("simulated crash between write and rename")
    monkeypatch.setattr(cameras_store.os, "replace", _boom)

    with pytest.raises(OSError):
        store.save([Camera(name="garbage", ip="2.2.2.2", password="y")])

    # Load should still return the original camera — atomic guarantee.
    cams = store.load()
    assert [c.name for c in cams] == ["original"]


def test_duplicate_name_rejected(data_dir):
    from app.cameras_store import Camera, CameraStore, CameraValidationError
    store = CameraStore()
    store.create(Camera(name="cam", ip="192.168.1.100", password="x"))
    with pytest.raises(CameraValidationError):
        store.create(Camera(name="cam", ip="192.168.1.101", password="y"))


@pytest.mark.parametrize("bad_name", [
    "Living Room",     # space + uppercase
    "1cam",            # starts with digit
    "cam-name",        # hyphen
    "",                # empty
    "x" * 32,          # too long
])
def test_invalid_name_rejected(data_dir, bad_name):
    from app.cameras_store import Camera, CameraStore, CameraValidationError
    store = CameraStore()
    with pytest.raises(CameraValidationError):
        store.create(Camera(name=bad_name, ip="192.168.1.100", password="x"))


def test_missing_password_rejected(data_dir):
    from app.cameras_store import Camera, CameraStore, CameraValidationError
    store = CameraStore()
    with pytest.raises(CameraValidationError):
        store.create(Camera(name="cam", ip="192.168.1.100", password=""))


def test_invalid_port_rejected(data_dir):
    from app.cameras_store import Camera, CameraStore, CameraValidationError
    store = CameraStore()
    with pytest.raises(CameraValidationError):
        store.create(Camera(name="cam", ip="1.1.1.1", password="x", rtsp_port=70000))


def test_update_keeps_other_cameras(data_dir):
    from app.cameras_store import Camera, CameraStore
    store = CameraStore()
    store.create(Camera(name="a", ip="1.1.1.1", password="x"))
    store.create(Camera(name="b", ip="2.2.2.2", password="y"))
    store.update("a", Camera(name="a", ip="9.9.9.9", password="z"))
    cams = {c.name: c for c in store.load()}
    assert cams["a"].ip == "9.9.9.9"
    assert cams["b"].ip == "2.2.2.2"


def test_update_missing_raises_keyerror(data_dir):
    from app.cameras_store import Camera, CameraStore
    store = CameraStore()
    with pytest.raises(KeyError):
        store.update("ghost", Camera(name="ghost", ip="1.1.1.1", password="x"))


def test_delete(data_dir):
    from app.cameras_store import Camera, CameraStore
    store = CameraStore()
    store.create(Camera(name="cam", ip="1.1.1.1", password="x"))
    assert store.delete("cam") is True
    assert store.delete("cam") is False
    assert store.load() == []


def test_template_view_url_encodes(data_dir):
    from app.cameras_store import Camera
    c = Camera(name="cam", ip="1.1.1.1", password='p@ss"word', user="ad min")
    view = c.template_view()
    assert view["password_url"] == "p%40ss%22word"
    assert view["user_url"] == "ad%20min"
    assert view["password"] == 'p@ss"word'  # bare password preserved for ONVIF
