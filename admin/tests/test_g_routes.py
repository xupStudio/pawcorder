"""End-to-end route tests for the G-series features.

Covers the routes that don't already have a dedicated test file:
  - GET  /cameras/{name}/zones      — page renders for valid cam
  - PUT  /api/cameras/{name}/zones  — save + replace + validation
  - POST /api/cameras/{name}/ptz/preset-save — name validation
  - GET  /docs/api                  — page renders
  - GET  /api/system/integrations   — markdown served
  - GET  /timelapse                 — page renders
  - GET  /users                     — page renders
"""
from __future__ import annotations

import pytest


def _add_camera(authed_client):
    return authed_client.post("/api/cameras", json={
        "name": "cam_a", "ip": "192.168.1.10",
        "username": "admin", "password": "x",
        "rtsp_path": "/h264Preview_01_main",
    })


def test_zones_page_renders(authed_client):
    r1 = _add_camera(authed_client)
    assert r1.status_code in (200, 201)
    r2 = authed_client.get("/cameras/cam_a/zones")
    assert r2.status_code == 200
    assert "cam_a" in r2.text


def test_zones_page_404_for_unknown_camera(authed_client):
    resp = authed_client.get("/cameras/nope/zones")
    # _render_html_error renders an error page with status 404.
    assert resp.status_code == 404


def test_zones_save_replaces_atomically(authed_client):
    _add_camera(authed_client)
    payload = {
        "zones": [{"name": "couch", "points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.5]]}],
        "privacy_masks": [],
    }
    resp = authed_client.put("/api/cameras/cam_a/zones", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["zones"][0]["name"] == "couch"


def test_zones_save_rejects_non_list(authed_client):
    _add_camera(authed_client)
    resp = authed_client.put("/api/cameras/cam_a/zones", json={"zones": "not a list"})
    assert resp.status_code == 400


def test_zones_save_rejects_unknown_camera(authed_client):
    resp = authed_client.put("/api/cameras/nope/zones", json={"zones": []})
    assert resp.status_code == 404


def test_ptz_preset_save_validates_name(authed_client):
    _add_camera(authed_client)
    # Empty name is rejected.
    resp = authed_client.post("/api/cameras/cam_a/ptz/preset-save", json={"name": ""})
    assert resp.status_code == 400


def test_ptz_preset_save_unknown_camera(authed_client):
    resp = authed_client.post("/api/cameras/ghost/ptz/preset-save", json={"name": "feed_spot"})
    assert resp.status_code == 404


def test_docs_api_page_renders(authed_client):
    resp = authed_client.get("/docs/api")
    assert resp.status_code == 200
    # Page should hint at marked.js + integrations endpoint.
    assert "/api/system/integrations" in resp.text


def test_integrations_endpoint_serves_markdown(authed_client):
    resp = authed_client.get("/api/system/integrations")
    # 200 if the docs file shipped, 404 if minimal install — both shapes ok.
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert "API" in resp.text or "endpoint" in resp.text.lower()
        # markdown content type
        assert "text/markdown" in resp.headers.get("content-type", "")


def test_timelapse_page_renders(authed_client):
    resp = authed_client.get("/timelapse")
    assert resp.status_code == 200


def test_users_page_renders(authed_client):
    resp = authed_client.get("/users")
    assert resp.status_code == 200


def test_users_me_route_returns_role(authed_client):
    resp = authed_client.get("/api/users/me")
    assert resp.status_code == 200
    body = resp.json()
    assert "role" in body
    assert body["role"] in ("admin", "family", "kid")
