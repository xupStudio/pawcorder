"""Tests for the Frigate webhook receiver — auth-free + dedup."""
from __future__ import annotations


def _payload(event_id="evt-1"):
    return {
        "type": "new",
        "after": {
            "id": event_id, "camera": "kitchen", "label": "cat",
            "score": 0.91, "top_score": 0.93,
            "start_time": 1234567890.0,
        },
    }


def test_webhook_no_auth_required(app_client):
    """Frigate posts from a different network identity than a logged-in
    user; auth-required would block it."""
    resp = app_client.post(
        "/api/frigate/event", json=_payload(),
        headers={"X-Requested-With": ""},  # also no CSRF — webhook is exempt
    )
    assert resp.status_code in (200, 500)  # 500 only if telegram throws; 200 expected


def test_webhook_invalid_payload(app_client):
    resp = app_client.post(
        "/api/frigate/event", json={"after": "not-a-dict"},
    )
    assert resp.status_code == 400


def test_webhook_dedup_same_event_id(app_client):
    """Posting the same event_id twice → second call is skipped."""
    from app import main
    # Reset module-level dedup set so this test is hermetic.
    main._webhook_handled_events.clear()

    resp1 = app_client.post("/api/frigate/event", json=_payload("dedup-evt"))
    resp2 = app_client.post("/api/frigate/event", json=_payload("dedup-evt"))
    assert resp1.status_code in (200, 500)
    assert resp2.status_code == 200
    assert resp2.json().get("skipped") == "duplicate"


def test_webhook_unknown_event_type_skipped(app_client):
    payload = _payload()
    payload["type"] = "frobnicate"
    resp = app_client.post("/api/frigate/event", json=payload)
    assert resp.status_code == 200
    assert "skipped" in resp.json()


def test_webhook_no_event_id_skipped(app_client):
    payload = _payload()
    payload["after"].pop("id")
    resp = app_client.post("/api/frigate/event", json=payload)
    assert resp.status_code == 200
    assert resp.json().get("skipped") == "no event id"
