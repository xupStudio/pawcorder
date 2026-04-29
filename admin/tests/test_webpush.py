"""Tests for VAPID keypair + subscription store + send."""
from __future__ import annotations


def test_vapid_keypair_generates_once(data_dir):
    """First call generates; second reads from disk and returns the
    same public key. Rotating would break every subscriber, so this
    matters."""
    from app import webpush
    pair_a = webpush.load_or_create_keypair()
    if pair_a is None:
        return  # cryptography not installed in this env — skip
    pair_b = webpush.load_or_create_keypair()
    assert pair_a.public_key_b64 == pair_b.public_key_b64
    assert pair_a.private_key_pem == pair_b.private_key_pem


def test_public_key_b64_matches_keypair(data_dir):
    from app import webpush
    pair = webpush.load_or_create_keypair()
    if pair is None:
        return
    assert webpush.public_key_b64() == pair.public_key_b64


def test_add_then_list_subscription(data_dir):
    from app import webpush
    sub = webpush.add_subscription(
        endpoint="https://push.example/abc",
        p256dh="aaaa", auth="bbbb", user_agent="test/1.0",
    )
    assert sub.endpoint == "https://push.example/abc"
    listing = webpush.list_subscriptions()
    assert len(listing) == 1
    assert listing[0].auth == "bbbb"


def test_add_subscription_dedupes_by_endpoint(data_dir):
    """Re-subscribing the same endpoint replaces the previous record
    rather than creating two."""
    from app import webpush
    webpush.add_subscription("https://e/1", "k1", "a1")
    webpush.add_subscription("https://e/1", "k2", "a2")
    listing = webpush.list_subscriptions()
    assert len(listing) == 1
    assert listing[0].auth == "a2"


def test_remove_subscription(data_dir):
    from app import webpush
    webpush.add_subscription("https://e/1", "k", "a")
    assert webpush.remove_subscription("https://e/1") is True
    assert webpush.list_subscriptions() == []
    assert webpush.remove_subscription("https://e/1") is False


def test_send_to_all_no_subs_is_noop(data_dir):
    from app import webpush
    out = webpush.send_to_all("title", "body")
    assert out["sent"] == 0


# ---- routes ------------------------------------------------------------

def test_subscribe_route_validates_payload(authed_client):
    resp = authed_client.post("/api/webpush/subscribe", json={"subscription": {}})
    assert resp.status_code == 400


def test_subscribe_route_happy_path(authed_client):
    payload = {"subscription": {
        "endpoint": "https://push.example/abc",
        "keys": {"p256dh": "K", "auth": "A"},
    }}
    resp = authed_client.post("/api/webpush/subscribe", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_public_key_route(authed_client):
    resp = authed_client.get("/api/webpush/public-key")
    assert resp.status_code == 200
    assert "public_key" in resp.json()


def test_test_route_runs_without_subs(authed_client):
    """Empty store → /test responds with sent=0, no exception."""
    resp = authed_client.post("/api/webpush/test", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] == 0
