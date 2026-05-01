"""Health check / monitor tests."""
from __future__ import annotations

from pathlib import Path


def test_check_storage_under_threshold(data_dir):
    from app import health
    res = health.check_storage(str(data_dir), warn_fraction=0.99)
    assert res.ok
    assert res.detail["path"] == str(data_dir)


def test_check_storage_missing_path_returns_ok(data_dir):
    from app import health
    res = health.check_storage("/no/such/path/here-1234")
    # Missing path means fresh install — don't yell.
    assert res.ok
    assert "not accessible" in res.message or res.message == "OK"


def test_check_storage_empty_path_ok(data_dir):
    from app import health
    res = health.check_storage("")
    assert res.ok


def test_check_storage_over_threshold_triggers(data_dir, monkeypatch):
    """Force-feed shutil.disk_usage to look 95% full."""
    from app import health

    class FakeUsage:
        total = 1000
        used = 950
        free = 50

    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: FakeUsage())
    res = health.check_storage(str(data_dir), warn_fraction=0.9)
    assert not res.ok
    assert "95" in res.message  # surfaces the percentage


def test_check_frigate_uses_docker_status(data_dir, stub_docker):
    from app import health
    res = health.check_frigate()
    assert res.ok  # stub_docker says running + healthy


def test_check_frigate_offline(data_dir, monkeypatch):
    from app import health, docker_ops
    from app.docker_ops import ContainerStatus

    monkeypatch.setattr(
        docker_ops, "get_frigate_status",
        lambda: ContainerStatus(name="t", exists=True, running=False,
                                status="exited", health=None, image="x"),
    )
    res = health.check_frigate()
    assert not res.ok


def test_health_route_returns_snapshot(authed_client):
    resp = authed_client.get("/api/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "overall_ok" in body
    assert "storage" in body
    assert "frigate" in body


def test_health_overall_ok_when_all_pass(data_dir, stub_docker, monkeypatch):
    """Snapshot composes correctly when every check is OK."""
    import asyncio
    from app import health

    async def _no_cams():
        return []

    monkeypatch.setattr(health, "check_cameras", _no_cams)
    snap = asyncio.run(health.snapshot())
    assert snap.overall_ok
    assert snap.storage.ok
    assert snap.frigate.ok


# ---- check_cameras: latest.jpg + Last-Modified -------------------------

class _FakeResp:
    """Minimal stand-in for httpx.Response used in check_cameras stubs."""
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeClient:
    """Plays both /api/config (GET) and /api/<cam>/latest.jpg (HEAD)."""
    def __init__(self, *, cams, head_responses):
        self._cams = cams
        self._heads = head_responses
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return False
    async def get(self, url):
        # /api/config
        if url.endswith("/api/config"):
            class _R(_FakeResp):
                def json(self):
                    return {"cameras": {c: {} for c in self._cams}}
            r = _R(status_code=200)
            r._cams = self._cams
            r.json = lambda: {"cameras": {c: {} for c in self._cams}}
            return r
        return _FakeResp(status_code=404)
    async def head(self, url, follow_redirects=False):
        # url ends with /api/<name>/latest.jpg
        for cam, resp in self._heads.items():
            if f"/api/{cam}/latest.jpg" in url:
                return resp
        return _FakeResp(status_code=404)


def test_check_cameras_marks_stale_offline(data_dir, monkeypatch):
    """A camera whose Last-Modified is 10 minutes old → ok=False."""
    import asyncio
    import email.utils
    import time
    from app import health

    stale = email.utils.formatdate(time.time() - 600, usegmt=True)  # 10 min ago
    fresh = email.utils.formatdate(time.time() - 5, usegmt=True)    # 5 sec ago

    def _client_factory(*args, **kwargs):
        return _FakeClient(
            cams=["cam_a", "cam_b"],
            head_responses={
                "cam_a": _FakeResp(200, {"Last-Modified": stale}),
                "cam_b": _FakeResp(200, {"Last-Modified": fresh}),
            },
        )
    monkeypatch.setattr(health.httpx, "AsyncClient", _client_factory)

    results = asyncio.run(health.check_cameras())
    by_name = {r.name: r for r in results}
    assert by_name["camera:cam_a"].ok is False
    assert "no fresh frame" in by_name["camera:cam_a"].message
    assert by_name["camera:cam_b"].ok is True


def test_check_cameras_handles_404(data_dir, monkeypatch):
    """latest.jpg returning 404 → ok=False with status in detail."""
    import asyncio
    from app import health

    def _client_factory(*args, **kwargs):
        return _FakeClient(
            cams=["dead_cam"],
            head_responses={"dead_cam": _FakeResp(404, {})},
        )
    monkeypatch.setattr(health.httpx, "AsyncClient", _client_factory)

    results = asyncio.run(health.check_cameras())
    assert results[0].ok is False
    assert "404" in results[0].message


def test_check_cameras_no_config_returns_empty(data_dir, monkeypatch):
    """Frigate not yet running (config endpoint dies) → empty list, no alerts."""
    import asyncio
    from app import health

    class _DownClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, url):
            raise health.httpx.HTTPError("connection refused")
    monkeypatch.setattr(health.httpx, "AsyncClient", lambda *a, **k: _DownClient())

    results = asyncio.run(health.check_cameras())
    assert results == []


def test_check_cameras_handles_missing_last_modified(data_dir, monkeypatch):
    """200 with no Last-Modified header → treat as OK (we have nothing
    to alert on; the previous heuristic would have spuriously flagged)."""
    import asyncio
    from app import health

    def _client_factory(*args, **kwargs):
        return _FakeClient(
            cams=["cam_x"],
            head_responses={"cam_x": _FakeResp(200, {})},
        )
    monkeypatch.setattr(health.httpx, "AsyncClient", _client_factory)

    results = asyncio.run(health.check_cameras())
    assert results[0].ok is True


# ---- alert cooldown reset on recovery ----------------------------------

def test_alert_cooldown_resets_on_recovery(data_dir, monkeypatch):
    """Cooldown should reset when a check goes back to OK so the *next*
    failure alerts again rather than being silenced."""
    import asyncio
    import time
    from app import health, telegram as tg, config_store

    # Enable Telegram so the alerter wants to send.
    cfg = config_store.load_config()
    cfg.telegram_enabled = True
    cfg.telegram_bot_token = "t"
    cfg.telegram_chat_id = "1"
    config_store.save_config(cfg)

    sent = []
    async def _send(_token, _chat, text):
        sent.append(text)
    monkeypatch.setattr(tg, "send_message", _send)

    monitor = health.HealthMonitor()
    bad = health.HealthSnapshot(
        storage=health.CheckResult(name="storage", ok=False, message="full"),
        frigate=health.CheckResult(name="frigate", ok=True, message="OK"),
        cameras=[], overall_ok=False, checked_at=time.time(),
    )
    good = health.HealthSnapshot(
        storage=health.CheckResult(name="storage", ok=True, message="OK"),
        frigate=health.CheckResult(name="frigate", ok=True, message="OK"),
        cameras=[], overall_ok=True, checked_at=time.time(),
    )

    asyncio.run(monitor._maybe_alert(bad))   # alert 1
    asyncio.run(monitor._maybe_alert(bad))   # cooldown silences it
    asyncio.run(monitor._maybe_alert(good))  # recovery resets cooldown
    asyncio.run(monitor._maybe_alert(bad))   # next failure re-alerts

    assert len(sent) == 2  # not 3 (cooldown) and not 1 (no recovery reset)
    # Each alert is in HTML format with the failing check's message embedded.
    for msg in sent:
        assert "<b>Pawcorder health</b>" in msg
        assert "full" in msg


def test_alert_silenced_when_telegram_disabled(data_dir, monkeypatch):
    """If the user hasn't configured Telegram, _maybe_alert silently
    does nothing — no exceptions, no spurious sends."""
    import asyncio
    import time
    from app import health, telegram as tg

    sent = []
    async def _send(*args, **kwargs):
        sent.append(args)
    monkeypatch.setattr(tg, "send_message", _send)

    monitor = health.HealthMonitor()
    bad = health.HealthSnapshot(
        storage=health.CheckResult(name="storage", ok=False, message="full"),
        frigate=health.CheckResult(name="frigate", ok=True, message="OK"),
        cameras=[], overall_ok=False, checked_at=time.time(),
    )
    asyncio.run(monitor._maybe_alert(bad))
    assert sent == []
