"""Shared fixtures for pawcorder admin tests.

A fresh temp data directory per test, so cameras.yml / .env state is
isolated. We also stub out Docker (no daemon needed in CI), Reolink HTTP
(we never make real network calls), and the Telegram poller (no live API).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ADMIN_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ADMIN_DIR.parent
TEMPLATE_SRC = PROJECT_ROOT / "config" / "frigate.template.yml"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a self-contained PAWCORDER_DATA_DIR for one test."""
    (tmp_path / "config").mkdir()
    shutil.copy(TEMPLATE_SRC, tmp_path / "config" / "frigate.template.yml")
    (tmp_path / ".env").write_text(
        'STORAGE_PATH="/tmp/storage"\n'
        'FRIGATE_RTSP_PASSWORD="testpw"\n'
        'TZ="UTC"\n'
        'PET_MIN_SCORE="0.65"\n'
        'PET_THRESHOLD="0.70"\n'
        'ADMIN_PASSWORD="test"\n'
        'ADMIN_SESSION_SECRET="test-secret-do-not-use"\n'
        'TAILSCALE_HOSTNAME=""\n'
        'TELEGRAM_ENABLED="0"\n'
        'TELEGRAM_BOT_TOKEN=""\n'
        'TELEGRAM_CHAT_ID=""\n'
        'LINE_ENABLED="0"\n'
        'LINE_CHANNEL_TOKEN=""\n'
        'LINE_TARGET_ID=""\n'
        'ADMIN_LANG="en"\n'
        'TRACK_CAT="1"\n'
        'TRACK_DOG="1"\n'
        'TRACK_PERSON="1"\n'
    )
    monkeypatch.setenv("PAWCORDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FRIGATE_CONTAINER", "test-frigate")
    _purge_app_modules()
    return tmp_path


def _purge_app_modules() -> None:
    """Remove app + app.* from sys.modules so subsequent imports re-evaluate
    module-level constants like DATA_DIR. Popping only `app.*` leaves the
    package object holding stale submodule attributes."""
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            sys.modules.pop(mod, None)


@pytest.fixture
def stub_docker(monkeypatch: pytest.MonkeyPatch):
    """Replace docker_ops functions so tests don't hit a real daemon."""
    import app.docker_ops as docker_ops
    from app.docker_ops import ContainerStatus

    def _status() -> ContainerStatus:
        return ContainerStatus(
            name="test-frigate", exists=True, running=True,
            status="running", health="healthy", image="frigate:test",
        )

    monkeypatch.setattr(docker_ops, "get_frigate_status", _status)
    monkeypatch.setattr(docker_ops, "restart_frigate", lambda: None)
    monkeypatch.setattr(docker_ops, "recent_frigate_logs", lambda tail=200: "log line\n")
    return docker_ops


@pytest.fixture
def stub_reolink(monkeypatch: pytest.MonkeyPatch):
    """Stub Reolink HTTP API and ffprobe so tests run offline."""
    import app.camera_api as camera_api
    from app.camera_api import RtspProbeResult

    async def _auto(ip: str, user: str, password: str) -> dict:
        if password == "wrong":
            raise PermissionError("invalid password")
        return {
            "device": {"model": "E1 Outdoor PoE", "firmVer": "test"},
            "link": {"activeLink": "WiFi" if ip.endswith(".101") else "LAN"},
            "connection_type": "wifi" if ip.endswith(".101") else "wired",
        }

    async def _probe(url: str, timeout_seconds: int = 8) -> RtspProbeResult:
        return RtspProbeResult(ok=True, codec="h264", width=1920, height=1080)

    monkeypatch.setattr(camera_api, "auto_configure", _auto)
    monkeypatch.setattr(camera_api, "probe_rtsp", _probe)
    return camera_api


@pytest.fixture
def stub_telegram(monkeypatch: pytest.MonkeyPatch):
    """Disable background pollers (Telegram + cloud uploader) and stub send_test."""
    import app.telegram as tg
    import app.line as line_api
    import app.cloud as cloud
    from app.telegram import TelegramSendResult
    from app.line import LineSendResult

    async def _tg_test(*_, **__) -> TelegramSendResult:
        return TelegramSendResult(ok=True)

    async def _line_test(*_, **__) -> LineSendResult:
        return LineSendResult(ok=True)

    monkeypatch.setattr(tg, "send_test", _tg_test)
    monkeypatch.setattr(line_api, "send_test", _line_test)
    monkeypatch.setattr(tg.poller, "start", lambda: None)
    monkeypatch.setattr(cloud.uploader, "start", lambda: None)
    return tg


@pytest.fixture
def stub_network_scan(monkeypatch: pytest.MonkeyPatch):
    import app.network_scan as ns
    from app.network_scan import Candidate, _looks_like_cidr

    async def _scan(cidr: str, timeout_seconds: int = 60):
        # Preserve the real validation so route-level error handling is exercised.
        if not _looks_like_cidr(cidr):
            raise ValueError(f"Invalid CIDR: {cidr!r}")
        return [Candidate(ip="192.168.1.100"), Candidate(ip="192.168.1.101")]

    monkeypatch.setattr(ns, "scan_for_cameras", _scan)
    return ns


@pytest.fixture
def stub_camera_dispatcher(monkeypatch: pytest.MonkeyPatch):
    """Replace camera_setup.auto_configure_for_brand so non-Reolink brand
    selections in route-level tests don't leak to real httpx calls. Each
    per-vendor module has its own unit tests that exercise the wire
    contract; this fixture only cares about route-level integration."""
    import app.camera_setup as camera_setup

    async def _auto(brand: str, ip: str, user: str, password: str) -> dict:
        if password == "wrong":
            raise PermissionError(f"{brand} login failed: invalid credentials")
        if camera_setup.is_manual_brand(brand):
            return {
                "device": None, "link": None, "connection_type": "unknown",
                "rtsp_main": "", "rtsp_sub": "", "manual": True,
            }
        # Reolink delegates to camera_api so stub_reolink's wifi/wired
        # classification (based on .101 vs other IPs) is preserved for
        # tests that exercise that branch.
        if brand == "reolink":
            import app.camera_api as camera_api
            return await camera_api.auto_configure(ip, user, password)
        return {
            "device": {"model": f"{brand}-stub", "manufacturer": brand.title()},
            "link": None,
            "connection_type": "wired",
            "rtsp_main": f"rtsp://{user}:{password}@{ip}:554/main",
            "rtsp_sub":  f"rtsp://{user}:{password}@{ip}:554/sub",
        }

    monkeypatch.setattr(camera_setup, "auto_configure_for_brand", _auto)
    return camera_setup


@pytest.fixture
def app_client(data_dir, stub_docker, stub_reolink, stub_camera_dispatcher,
               stub_telegram, stub_network_scan):
    """A FastAPI TestClient with all external deps stubbed and a fresh data dir.

    All TestClient requests automatically carry the CSRF header
    `X-Requested-With: pawcorder` — same as the production frontend's
    api()/apiUpload() helpers. Tests that want to exercise the
    CSRF-rejection path can override the header explicitly.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, headers={"X-Requested-With": "pawcorder"}) as client:
        yield client


@pytest.fixture
def authed_client(app_client):
    """A logged-in TestClient (cookie set via /login)."""
    resp = app_client.post("/login", data={"password": "test"}, follow_redirects=False)
    assert resp.status_code == 303
    return app_client
