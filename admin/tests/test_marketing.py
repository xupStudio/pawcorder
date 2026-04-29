"""Tests for the public Pro waitlist signup endpoint + CSV store."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Each test starts with empty rate-limit buckets so cross-test
    pollution doesn't 429 the next case."""
    yield
    from app import marketing
    marketing.reset_rate_limits()


# ---- pure-function tests -----------------------------------------------

def test_record_signup_writes_csv(data_dir):
    from app import marketing
    r = marketing.record_signup(email="ivan@example.com", source="landing", ip="1.2.3.4")
    assert r.ok and not r.duplicate
    rows = marketing.list_signups()
    assert len(rows) == 1
    assert rows[0]["email"] == "ivan@example.com"
    assert rows[0]["source"] == "landing"


def test_record_signup_normalizes_email(data_dir):
    """Mixed case + whitespace should land in the CSV lowercased + trimmed."""
    from app import marketing
    marketing.record_signup(email="  Ivan@Example.COM  ", ip="1.1.1.1")
    rows = marketing.list_signups()
    assert rows[0]["email"] == "ivan@example.com"


def test_record_signup_duplicate_returns_ok_with_flag(data_dir):
    from app import marketing
    marketing.record_signup(email="ivan@example.com", ip="1.1.1.1")
    r2 = marketing.record_signup(email="IVAN@example.com", ip="1.1.1.1")
    assert r2.ok and r2.duplicate
    # No second row written.
    assert len(marketing.list_signups()) == 1


@pytest.mark.parametrize("bad_email", [
    "", "notanemail", "@example.com", "ivan@", "ivan@example",
    "x" * 260 + "@example.com",  # too long
])
def test_record_signup_invalid_email_rejected(data_dir, bad_email):
    from app import marketing
    r = marketing.record_signup(email=bad_email, ip="1.1.1.1")
    assert not r.ok
    assert r.error == "invalid email"


def test_record_signup_rate_limits_per_ip(data_dir):
    from app import marketing
    ip = "9.9.9.9"
    for i in range(marketing.RATE_LIMIT_PER_IP):
        r = marketing.record_signup(email=f"user{i}@example.com", ip=ip)
        assert r.ok
    # The next signup from the same IP must be rate-limited.
    r = marketing.record_signup(email="another@example.com", ip=ip)
    assert not r.ok
    assert r.rate_limited


def test_record_signup_normalizes_source(data_dir):
    """source must be stripped of weird chars + capped in length."""
    from app import marketing
    marketing.record_signup(
        email="ivan@example.com",
        source="<script>alert</script>",
        ip="1.1.1.1",
    )
    rows = marketing.list_signups()
    assert rows[0]["source"] == "scriptalertscript"
    assert len(rows[0]["source"]) <= marketing.MAX_SOURCE_LEN


def test_record_signup_csv_has_header(data_dir):
    from app import marketing
    marketing.record_signup(email="a@example.com", ip="1.1.1.1")
    text = marketing.SIGNUPS_CSV.read_text()
    assert text.splitlines()[0] == ",".join(marketing.CSV_HEADER)


# ---- HTTP route tests --------------------------------------------------

def test_signup_route_no_auth_required(app_client):
    """The whole point — anonymous users on the landing page can post."""
    resp = app_client.post(
        "/api/marketing/signup",
        json={"email": "anon@example.com"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_signup_route_no_csrf_required(app_client):
    """Public form — same-origin only via CORS preflight, no header needed."""
    resp = app_client.post(
        "/api/marketing/signup",
        json={"email": "anon2@example.com"},
        headers={"X-Requested-With": ""},
    )
    assert resp.status_code == 200


def test_signup_route_invalid_email_400(app_client):
    resp = app_client.post(
        "/api/marketing/signup",
        json={"email": "not-an-email"},
    )
    assert resp.status_code == 400
    assert "invalid email" in resp.json()["error"]


def test_signup_route_rate_limit_429(app_client):
    from app import marketing
    for i in range(marketing.RATE_LIMIT_PER_IP):
        app_client.post("/api/marketing/signup",
                        json={"email": f"u{i}@example.com"})
    resp = app_client.post("/api/marketing/signup",
                            json={"email": "extra@example.com"})
    assert resp.status_code == 429


def test_signup_route_options_preflight(app_client):
    resp = app_client.options("/api/marketing/signup")
    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")


def test_signup_route_post_includes_cors_header(app_client):
    """POST response itself must carry CORS — preflight alone is not
    enough for the browser to deliver the response body cross-origin."""
    resp = app_client.post(
        "/api/marketing/signup",
        json={"email": "cors@example.com"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_signup_route_error_response_also_carries_cors(app_client):
    """Even the 400/429 paths must return CORS or browsers can't read
    the error body."""
    resp = app_client.post(
        "/api/marketing/signup",
        json={"email": "not-valid"},
    )
    assert resp.status_code == 400
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_signups_export_requires_auth(app_client):
    resp = app_client.get("/api/marketing/signups")
    assert resp.status_code == 401


def test_signups_export_returns_rows(authed_client):
    from app import marketing
    marketing.record_signup(email="a@example.com", ip="1.1.1.1")
    marketing.record_signup(email="b@example.com", ip="1.1.1.2")
    resp = authed_client.get("/api/marketing/signups")
    assert resp.status_code == 200
    rows = resp.json()["signups"]
    assert {r["email"] for r in rows} == {"a@example.com", "b@example.com"}
