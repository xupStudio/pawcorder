"""Privacy mode tests — Tailscale presence + state persistence."""
from __future__ import annotations

import json


def test_load_state_default_is_off(data_dir):
    from app import privacy
    s = privacy.load_state()
    assert s.enabled is False
    assert s.auto_pause_when_home is False
    assert s.paused_now is False
    assert s.home_devices == []


def test_save_round_trip(data_dir):
    from app import privacy
    s = privacy.PrivacyState(
        enabled=True, auto_pause_when_home=True, paused_now=False,
        home_devices=["my-iphone", "my-laptop"],
    )
    privacy.save_state(s)
    loaded = privacy.load_state()
    assert loaded.enabled
    assert loaded.auto_pause_when_home
    assert loaded.home_devices == ["my-iphone", "my-laptop"]


def test_save_state_atomic_kill_preserves_old_data(data_dir, monkeypatch):
    """Simulate a crash between Path.write_text and os.replace.
    The previous on-disk content must survive."""
    from app import privacy

    privacy.save_state(privacy.PrivacyState(
        enabled=True, home_devices=["original-phone"],
    ))

    # Now sabotage os.replace: the second save should crash before
    # commit, but the previous file content must still be intact.
    def _boom(*_args, **_kw):
        raise OSError("simulated crash between write and rename")
    monkeypatch.setattr(privacy.os, "replace", _boom)

    import pytest
    with pytest.raises(OSError):
        privacy.save_state(privacy.PrivacyState(
            enabled=True, home_devices=["GARBAGE"],
        ))

    # Load should still see "original-phone" — the temp file exists but
    # the canonical path was never replaced.
    survived = privacy.load_state()
    assert survived.home_devices == ["original-phone"]


def test_is_paused_off_by_default(data_dir):
    from app import privacy
    assert privacy.is_paused() is False


def test_evaluate_async_disabled_returns_unpaused(data_dir):
    import asyncio
    from app import privacy

    s = privacy.PrivacyState(enabled=False)
    out = asyncio.run(privacy.evaluate_async(s))
    assert out.paused_now is False


def test_evaluate_async_manual_keeps_state(data_dir):
    """When auto-mode is OFF, evaluate doesn't override paused_now."""
    import asyncio
    from app import privacy

    s = privacy.PrivacyState(enabled=True, auto_pause_when_home=False, paused_now=True)
    out = asyncio.run(privacy.evaluate_async(s))
    assert out.paused_now is True
    assert "manual" in out.reason


def test_evaluate_async_auto_pause_no_tailscale_resumes(data_dir, monkeypatch):
    """No Tailscale hits → no home presence → don't pause."""
    import asyncio
    from app import privacy

    async def _empty():
        return []

    monkeypatch.setattr(privacy, "tailscale_devices_online", _empty)
    s = privacy.PrivacyState(
        enabled=True, auto_pause_when_home=True, paused_now=True,
        home_devices=["my-iphone"],
    )
    out = asyncio.run(privacy.evaluate_async(s))
    assert out.paused_now is False


def test_evaluate_async_auto_pause_when_home_device_online(data_dir, monkeypatch):
    import asyncio
    from app import privacy

    async def _online():
        return ["my-iphone", "stranger-laptop"]

    monkeypatch.setattr(privacy, "tailscale_devices_online", _online)
    s = privacy.PrivacyState(
        enabled=True, auto_pause_when_home=True, paused_now=False,
        home_devices=["my-iphone"],
    )
    out = asyncio.run(privacy.evaluate_async(s))
    assert out.paused_now is True
    assert "my-iphone" in out.reason


def test_privacy_route_get(authed_client):
    resp = authed_client.get("/api/privacy")
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body
    assert "paused_now" in body


def test_privacy_route_save(authed_client):
    resp = authed_client.post("/api/privacy", json={
        "enabled": True,
        "auto_pause_when_home": True,
        "home_devices": ["my-phone"],
    })
    assert resp.status_code == 200
    assert resp.json()["home_devices"] == ["my-phone"]


def test_privacy_route_rejects_non_list_devices(authed_client):
    resp = authed_client.post("/api/privacy", json={
        "home_devices": "not-a-list",
    })
    assert resp.status_code == 400


# ---- privacy actuation: rendered Frigate config respects is_paused() ----

def test_render_records_enabled_when_privacy_off(data_dir):
    """Default privacy state (off) → record.enabled: true in the rendered yaml."""
    from app import config_store
    from app.cameras_store import Camera

    cfg = config_store.load_config()
    cams = [Camera(name="cam", ip="1.2.3.4", password="p")]
    rendered = config_store.render_frigate_config(cfg, cams)
    assert "enabled: true" in rendered
    # Sanity: the line we actually care about is the one inside `record:`.
    record_block = rendered.split("record:", 1)[1].split("retain:", 1)[0]
    assert "enabled: true" in record_block


def test_render_records_disabled_when_privacy_paused(data_dir):
    """privacy paused → record.enabled: false in the rendered yaml."""
    from app import config_store, privacy
    from app.cameras_store import Camera

    privacy.save_state(privacy.PrivacyState(enabled=True, paused_now=True))
    cfg = config_store.load_config()
    cams = [Camera(name="cam", ip="1.2.3.4", password="p")]
    rendered = config_store.render_frigate_config(cfg, cams)
    record_block = rendered.split("record:", 1)[1].split("retain:", 1)[0]
    assert "enabled: false" in record_block


def test_privacy_paused_only_when_master_enabled(data_dir):
    """is_paused() must return False if the master toggle is off, even
    if paused_now is somehow True (defensive — UI shouldn't get there)."""
    from app import privacy
    privacy.save_state(privacy.PrivacyState(enabled=False, paused_now=True))
    assert privacy.is_paused() is False


# ---- privacy monitor: transitions trigger reapply, steady state does not ----

def test_monitor_reapplies_on_first_tick_then_steady_state(data_dir, monkeypatch):
    """First tick must reapply unconditionally — we can't trust that the
    rendered config.yml on disk matches privacy state across container
    restarts. Subsequent ticks are no-ops if the state hasn't changed."""
    import asyncio
    from app import privacy, docker_ops, cameras_store
    from app.cameras_store import Camera

    cameras_store.CameraStore().create(Camera(name="cam", ip="1.2.3.4", password="p"))

    calls = []
    monkeypatch.setattr(docker_ops, "restart_frigate", lambda: calls.append(1))

    async def _eval(state=None):
        return privacy.PrivacyState(enabled=True, auto_pause_when_home=True,
                                     paused_now=True, home_devices=["x"])
    monkeypatch.setattr(privacy, "evaluate_async", _eval)

    mon = privacy.PrivacyMonitor()

    async def _run_two_ticks():
        await mon._tick()
        await mon._tick()
    asyncio.run(_run_two_ticks())
    # Tick 1: reconcile with disk → 1 restart.
    # Tick 2: steady state → no extra restart.
    assert calls == [1]

    # Flip to unpaused — transition triggers exactly one more restart.
    async def _eval_off(state=None):
        return privacy.PrivacyState(enabled=True, auto_pause_when_home=True,
                                     paused_now=False)
    monkeypatch.setattr(privacy, "evaluate_async", _eval_off)
    asyncio.run(mon._tick())
    assert calls == [1, 1]


def test_monitor_first_tick_reapplies_even_when_unpaused(data_dir, monkeypatch):
    """Boundary: reconcile happens regardless of which side of the toggle
    we boot up on. Otherwise an admin restart could leave Frigate paused
    even though privacy says recording=on."""
    import asyncio
    from app import privacy, docker_ops, cameras_store
    from app.cameras_store import Camera

    cameras_store.CameraStore().create(Camera(name="cam", ip="1.2.3.4", password="p"))

    calls = []
    monkeypatch.setattr(docker_ops, "restart_frigate", lambda: calls.append(1))

    async def _eval(state=None):
        return privacy.PrivacyState(enabled=True, paused_now=False)  # not paused
    monkeypatch.setattr(privacy, "evaluate_async", _eval)

    mon = privacy.PrivacyMonitor()
    asyncio.run(mon._tick())
    assert calls == [1]


def test_monitor_retries_after_failed_reapply(data_dir, monkeypatch):
    """When _reapply raises, _last_acted must NOT be committed — so the
    next tick still sees a transition and tries again. Prevents permanent
    desync after a transient docker hiccup."""
    import asyncio
    from app import privacy, docker_ops, cameras_store
    from app.cameras_store import Camera

    cameras_store.CameraStore().create(Camera(name="cam", ip="1.2.3.4", password="p"))

    attempts = []

    def _maybe_boom():
        attempts.append(1)
        if len(attempts) <= 2:
            # Boot reconcile + first transition both fail.
            raise RuntimeError("docker daemon transient failure")
    monkeypatch.setattr(docker_ops, "restart_frigate", _maybe_boom)

    async def _eval_paused(state=None):
        return privacy.PrivacyState(enabled=True, paused_now=True)
    async def _eval_unpaused(state=None):
        return privacy.PrivacyState(enabled=True, paused_now=False)

    mon = privacy.PrivacyMonitor()
    monkeypatch.setattr(privacy, "evaluate_async", _eval_paused)
    asyncio.run(mon._tick())  # boot reconcile fails → _last_acted stays None

    # Note: docker_ops.restart_frigate raises RuntimeError, which _reapply
    # catches and turns into "skip restart" rather than re-raising. So
    # the boot tick still commits _last_acted=True. That's the right
    # behavior for "Frigate not yet started" — we don't want to retry
    # forever just because Frigate isn't up. The retry guarantee in this
    # test only kicks in for *non*-RuntimeError exceptions, which we
    # exercise next.

    def _real_boom():
        attempts.append(1)
        raise OSError("docker socket gone")
    monkeypatch.setattr(docker_ops, "restart_frigate", _real_boom)

    monkeypatch.setattr(privacy, "evaluate_async", _eval_unpaused)
    asyncio.run(mon._tick())  # transition; _reapply raises OSError
    monkeypatch.setattr(privacy, "evaluate_async", _eval_unpaused)
    asyncio.run(mon._tick())  # another tick — still considered a transition
    # Both ticks attempted to call restart_frigate (one initial + 2 OSError attempts).
    assert len(attempts) >= 3


def test_monitor_swallows_restart_errors(data_dir, monkeypatch):
    """A docker hiccup must not kill the privacy poller — _reapply
    catches RuntimeError so the loop keeps going."""
    import asyncio
    from app import privacy, docker_ops, cameras_store
    from app.cameras_store import Camera

    cameras_store.CameraStore().create(Camera(name="cam", ip="1.2.3.4", password="p"))

    def _boom():
        raise RuntimeError("docker daemon down")
    monkeypatch.setattr(docker_ops, "restart_frigate", _boom)

    async def _eval_paused(state=None):
        return privacy.PrivacyState(enabled=True, auto_pause_when_home=True,
                                     paused_now=True)
    async def _eval_unpaused(state=None):
        return privacy.PrivacyState(enabled=True, auto_pause_when_home=True,
                                     paused_now=False)

    mon = privacy.PrivacyMonitor()
    monkeypatch.setattr(privacy, "evaluate_async", _eval_paused)
    asyncio.run(mon._tick())
    monkeypatch.setattr(privacy, "evaluate_async", _eval_unpaused)
    # Should NOT raise.
    asyncio.run(mon._tick())


def test_monitor_skips_reapply_when_no_cameras(data_dir, monkeypatch):
    """During initial setup (no cameras yet), _reapply must noop instead
    of trying to render an empty Frigate config."""
    import asyncio
    from app import privacy, docker_ops

    calls = []
    monkeypatch.setattr(docker_ops, "restart_frigate", lambda: calls.append(1))

    async def _eval_paused(state=None):
        return privacy.PrivacyState(enabled=True, paused_now=True)
    async def _eval_unpaused(state=None):
        return privacy.PrivacyState(enabled=True, paused_now=False)

    mon = privacy.PrivacyMonitor()
    monkeypatch.setattr(privacy, "evaluate_async", _eval_paused)
    asyncio.run(mon._tick())
    monkeypatch.setattr(privacy, "evaluate_async", _eval_unpaused)
    asyncio.run(mon._tick())
    # 0 cameras → 0 restarts even on transition.
    assert calls == []


def test_monitor_start_creates_task_and_stop_cancels(data_dir, monkeypatch):
    """Lifecycle hooks: start() creates an asyncio.Task; stop() awaits it."""
    import asyncio
    from app import privacy

    async def _slow_eval(state=None):
        await asyncio.sleep(0.05)
        return privacy.PrivacyState(enabled=False)
    monkeypatch.setattr(privacy, "evaluate_async", _slow_eval)
    monkeypatch.setattr(privacy, "PRIVACY_POLL_INTERVAL_SECONDS", 0.01)

    async def _exercise():
        mon = privacy.PrivacyMonitor()
        mon.start()
        assert mon._task is not None
        await asyncio.sleep(0.1)  # let one tick happen
        await mon.stop()
        assert mon._task.done()
    asyncio.run(_exercise())
