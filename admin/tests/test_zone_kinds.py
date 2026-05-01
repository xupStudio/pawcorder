"""Tests for the zones[*].kind field + point-in-polygon helper.

These cover the schema migration (legacy zones get backfilled to
kind="detect") and the geometric primitive that the Pro detectors
use to decide if a sighting falls inside a tagged zone.
"""
from __future__ import annotations


def test_legacy_zone_backfills_to_detect(data_dir):
    from app import cameras_store
    cam = cameras_store.Camera.from_dict({
        "name": "cam_a", "ip": "192.168.1.10", "password": "x",
        # Legacy-shape zone — name + points only, no kind.
        "zones": [{"name": "old", "points": [[0, 0], [1, 0], [1, 1]]}],
    })
    assert cam.zones[0]["kind"] == "detect"
    # zone_kind() reads the same defaulted value back.
    assert cameras_store.zone_kind(cam.zones[0]) == "detect"


def test_explicit_kind_is_preserved_through_round_trip(data_dir):
    from app import cameras_store
    store = cameras_store.CameraStore()
    cam = cameras_store.Camera(
        name="cam_a", ip="192.168.1.10", password="x",
        zones=[
            {"name": "litter", "points": [[0, 0], [1, 0], [1, 1]],
             "kind": "litter_box"},
            {"name": "water", "points": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]],
             "kind": "water_bowl"},
        ],
    )
    store.create(cam)
    fresh = store.get("cam_a")
    assert fresh is not None
    kinds = sorted(z["kind"] for z in fresh.zones)
    assert kinds == ["litter_box", "water_bowl"]


def test_zone_kind_defaults_unknown_to_detect():
    from app import cameras_store
    assert cameras_store.zone_kind({"name": "x", "points": [],
                                     "kind": "lol_unknown"}) == "detect"
    assert cameras_store.zone_kind({"name": "x", "points": []}) == "detect"
    # A None / non-dict input would crash get(); the caller is expected
    # to have a real dict — but we still cover the empty-dict edge.
    assert cameras_store.zone_kind({}) == "detect"


def test_zones_of_kind_filters_correctly():
    from app import cameras_store
    cam = cameras_store.Camera(
        name="cam_a", ip="192.168.1.10", password="x",
        zones=[
            {"name": "a", "points": [[0, 0]], "kind": "detect"},
            {"name": "b", "points": [[0, 0]], "kind": "litter_box"},
            {"name": "c", "points": [[0, 0]], "kind": "litter_box"},
        ],
    )
    boxes = cameras_store.zones_of_kind(cam, "litter_box")
    assert [z["name"] for z in boxes] == ["b", "c"]


def test_point_in_zone_rectangle():
    from app import cameras_store
    rect = {"name": "r", "kind": "detect",
            "points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]}
    assert cameras_store.point_in_zone(rect, 0.5, 0.5) is True
    assert cameras_store.point_in_zone(rect, 0.1, 0.1) is False
    assert cameras_store.point_in_zone(rect, 0.9, 0.9) is False


def test_point_in_zone_degenerate_polygon_is_false():
    from app import cameras_store
    # < 3 points = no polygon
    assert cameras_store.point_in_zone({"points": []}, 0.5, 0.5) is False
    assert cameras_store.point_in_zone({"points": [[0, 0], [1, 0]]},
                                         0.5, 0.5) is False
    # malformed point pair
    assert cameras_store.point_in_zone(
        {"points": [["x", 0], [1, 0], [1, 1]]}, 0.5, 0.5,
    ) is False
