"""Run the pawcorder admin panel with mock data for UI preview.

External dependencies (Docker daemon, real Reolink camera, ffprobe) are
stubbed so this works on a laptop with nothing else running.

Usage (from the admin/ directory):

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python -m app.demo

Then open http://localhost:8080 in a browser. Password: demo
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

ADMIN_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ADMIN_DIR.parent

DEMO_DIR = Path(tempfile.mkdtemp(prefix="pawcorder-demo-"))
(DEMO_DIR / "config").mkdir(parents=True)
# Demo recordings + timelapse + highlights all land here. We can't use
# the real /mnt/pawcorder because (a) macOS dev hosts don't have /mnt and
# (b) it'd survive across demo runs and confuse subsequent ones. Tmp dir
# is wiped together with DEMO_DIR when the OS cleans /var/folders.
DEMO_STORAGE = DEMO_DIR / "storage"
DEMO_STORAGE.mkdir(parents=True)
shutil.copy(
    PROJECT_ROOT / "config" / "frigate.template.yml",
    DEMO_DIR / "config" / "frigate.template.yml",
)

(DEMO_DIR / ".env").write_text(
    f'STORAGE_PATH="{DEMO_STORAGE}"\n'
    'FRIGATE_RTSP_PASSWORD="demosecret"\n'
    'TZ="Asia/Taipei"\n'
    'PET_MIN_SCORE="0.65"\n'
    'PET_THRESHOLD="0.70"\n'
    'ADMIN_PASSWORD="demo"\n'
    'ADMIN_SESSION_SECRET="demo-only-do-not-use-in-prod"\n'
    'TAILSCALE_HOSTNAME="pawcorder-demo.tail-abcd.ts.net"\n'
    'TELEGRAM_ENABLED="1"\n'
    'TELEGRAM_BOT_TOKEN="123456:DEMO_TOKEN"\n'
    'TELEGRAM_CHAT_ID="987654321"\n'
    'LINE_ENABLED="0"\n'
    'LINE_CHANNEL_TOKEN=""\n'
    'LINE_TARGET_ID=""\n'
    'ADMIN_LANG="zh-TW"\n'
    'TRACK_CAT="1"\n'
    'TRACK_DOG="1"\n'
    'TRACK_PERSON="1"\n'
    'DETECTOR_TYPE="cpu"\n'
)

(DEMO_DIR / "config" / "cameras.yml").write_text(
    "cameras:\n"
    "  - name: living_room\n"
    "    ip: 192.168.1.100\n"
    "    user: admin\n"
    "    password: demopass\n"
    "    rtsp_port: 554\n"
    "    onvif_port: 8000\n"
    "    detect_width: 640\n"
    "    detect_height: 480\n"
    "    enabled: true\n"
    "    connection_type: wired\n"
    "  - name: kitchen\n"
    "    ip: 192.168.1.101\n"
    "    user: admin\n"
    "    password: demopass\n"
    "    rtsp_port: 554\n"
    "    onvif_port: 8000\n"
    "    detect_width: 640\n"
    "    detect_height: 480\n"
    "    enabled: true\n"
    "    connection_type: wifi\n"
    "  - name: garage\n"
    "    ip: 192.168.1.102\n"
    "    user: admin\n"
    "    password: demopass\n"
    "    rtsp_port: 554\n"
    "    onvif_port: 8000\n"
    "    detect_width: 640\n"
    "    detect_height: 480\n"
    "    enabled: false\n"
    "    connection_type: wired\n"
)

os.environ["PAWCORDER_DATA_DIR"] = str(DEMO_DIR)

# Imports below depend on PAWCORDER_DATA_DIR being set.
from app import camera_api, docker_ops, line as line_api, main, network_scan, telegram as tg  # noqa: E402
from app.camera_api import RtspProbeResult  # noqa: E402
from app.docker_ops import ContainerStatus  # noqa: E402
from app.line import LineSendResult  # noqa: E402
from app.network_scan import Candidate  # noqa: E402
from app.telegram import TelegramSendResult  # noqa: E402


def _mock_status() -> ContainerStatus:
    return ContainerStatus(
        name="pawcorder-frigate",
        exists=True,
        running=True,
        status="running",
        health="healthy",
        image="ghcr.io/blakeblackshear/frigate:stable",
    )


docker_ops.get_frigate_status = _mock_status
docker_ops.restart_frigate = lambda: None
docker_ops.recent_frigate_logs = lambda tail=200: (
    "2026-04-28 12:00:00 [INFO] frigate.app                    : Starting Frigate (0.15.0)\n"
    "2026-04-28 12:00:01 [INFO] frigate.config                 : Loaded config from /config/config.yml\n"
    "2026-04-28 12:00:02 [INFO] frigate.detectors.openvino     : Initialized OpenVINO on /dev/dri (GPU)\n"
    "2026-04-28 12:00:03 [INFO] frigate.video                  : Camera living_room: 1920x1080 @ 15 fps (h264)\n"
    "2026-04-28 12:00:03 [INFO] frigate.video                  : Camera kitchen:     1920x1080 @ 15 fps (h264)\n"
    "2026-04-28 12:00:03 [INFO] frigate.video                  : Camera garage:      disabled\n"
    "2026-04-28 12:00:10 [INFO] frigate.events                 : Started event: living_room/cat\n"
    "2026-04-28 12:00:34 [INFO] frigate.events                 : Ended event: living_room/cat (24.1s, score 0.83)\n"
    "2026-04-28 12:01:55 [INFO] frigate.events                 : Started event: kitchen/dog\n"
    "2026-04-28 12:02:18 [INFO] frigate.events                 : Ended event: kitchen/dog (23.4s, score 0.91)\n"
)


async def _mock_auto_configure(ip: str, user: str, password: str) -> dict:
    if password == "wrong":
        raise PermissionError("Reolink login failed: invalid credentials")
    is_wifi = ip.endswith(".101") or "wifi" in ip
    return {
        "device": {"model": "E1 Outdoor PoE", "firmVer": "v3.1.0.4321", "name": "demo cam"},
        "link": {"activeLink": "WiFi" if is_wifi else "LAN"},
        "connection_type": "wifi" if is_wifi else "wired",
    }


async def _mock_probe_rtsp(url: str, timeout_seconds: int = 8) -> RtspProbeResult:
    return RtspProbeResult(ok=True, codec="h264", width=1920, height=1080)


# Brand-aware dispatcher stub: in demo mode the user can pick any brand
# from the dropdown, but we don't want clicks on Test/Save to actually
# reach a Hikvision/Dahua/Axis/Foscam/UniFi/ONVIF endpoint over the
# network. Replace the dispatcher with a fixed deterministic response so
# the entire flow stays offline.
async def _mock_auto_configure_for_brand(brand: str, ip: str, user: str, password: str) -> dict:
    if password == "wrong":
        raise PermissionError(f"{brand} login failed: invalid credentials")
    base = await _mock_auto_configure(ip, user, password)
    base.setdefault("rtsp_main", f"rtsp://{user}:{password}@{ip}:554/main")
    base.setdefault("rtsp_sub",  f"rtsp://{user}:{password}@{ip}:554/sub")
    return base


camera_api.auto_configure = _mock_auto_configure
camera_api.probe_rtsp = _mock_probe_rtsp
# Patch the dispatcher entry point so non-Reolink brand selections don't
# leak out to real httpx calls.
from app import camera_setup  # noqa: E402  -- after camera_api stubs land
camera_setup.auto_configure_for_brand = _mock_auto_configure_for_brand


async def _mock_scan(cidr: str, timeout_seconds: int = 60):
    return [
        Candidate(ip="192.168.1.100"),
        Candidate(ip="192.168.1.101"),
        Candidate(ip="192.168.1.102"),
        Candidate(ip="192.168.1.110"),
    ]


network_scan.scan_for_cameras = _mock_scan


# Telegram: stub the send_test so the "Send test message" button always
# succeeds without real network calls, and disable the background poller
# (no real Frigate to poll in demo mode).
async def _mock_send_test_tg(token: str, chat_id: str) -> TelegramSendResult:
    return TelegramSendResult(ok=True)

async def _mock_send_test_line(token: str, target: str) -> LineSendResult:
    return LineSendResult(ok=True)

tg.send_test = _mock_send_test_tg
line_api.send_test = _mock_send_test_line
tg.poller.start = lambda: None  # type: ignore[assignment]

# Stub the cloud uploader the same way (no real rclone in demo).
from app import cloud as cloud_module  # noqa: E402
cloud_module.uploader.start = lambda: None  # type: ignore[assignment]

# Pretend we have a connected Google Drive with 200 GB total, 50 GB used,
# 6 GB of which is pawcorder's. Lets the /cloud usage card light up.
async def _mock_run_rclone(*args, timeout=30):
    if "about" in args:
        return 0, '{"total":214748364800,"used":53687091200,"free":161061273600}', ""
    if "size" in args:
        return 0, '{"count":12,"bytes":6442450944}', ""
    if args and args[0] == "lsd":
        return 0, "          -1 2026-04-28 00:00:00         0 pawcorder\n", ""
    return 0, "", ""

cloud_module._run_rclone = _mock_run_rclone  # type: ignore[assignment]
cloud_module.save_remote("pawcorder", {"type": "drive", "scope": "drive", "token": "demo-token"})


# ---- demo seed: pets, sightings, diaries, anomaly highlights ---------
#
# Without seeded data the /pets page and the new health/litter/fight
# widgets all show empty states (their own sections hide). For a UI
# preview we want the user to immediately see what the production
# system surfaces — so we drop in two pets, ~80 sightings spread across
# the last 36h with intentional anomaly patterns, and a recent diary
# entry per pet. None of this touches recognition / Frigate paths; it
# just writes the storage formats those modules read.

def _seed_demo_data() -> None:
    import time
    from app import pets_store, recognition, pet_diary

    store = pets_store.PetStore()
    if not store.load():
        # Two cats — the multi-pet heuristic prior + fight detector
        # both need ≥2 pets to do anything visible.
        store.create(name="Mochi", species="cat",
                      notes="Black short-hair, age 6.")
        store.create(name="Maru",  species="cat",
                      notes="Tabby, age 3.")

    # Sentinel marks "we've seeded" without depending on file size —
    # avoids re-seeding on top of a half-written demo log from a crash.
    sentinel = recognition.SIGHTINGS_LOG.parent / ".demo-seeded"
    if sentinel.exists():
        return

    now = time.time()
    eid = 0

    def push(*, pet_id: str, pet_name: str, camera: str, ts: float,
             confidence: str = "high", score: float = 0.91):
        nonlocal eid
        eid += 1
        # Route through append_sighting so the on-disk format tracks
        # whatever schema recognition currently writes — demo can't
        # silently drift if we add fields later.
        recognition.append_sighting(recognition.Sighting(
            event_id=f"demo-{eid}", camera=camera, label="cat",
            pet_id=pet_id, pet_name=pet_name,
            score=score, confidence=confidence,
            start_time=ts, end_time=ts + 6,
            bbox=(120.0 + (eid % 80), 200.0, 180.0, 160.0),
        ))

    for d_off in (2, 1, 0):
        for h in (2, 3, 4, 5, 6):
            ts = now - d_off * 86400 - (24 - h) * 3600
            push(pet_id="mochi", pet_name="Mochi",
                 camera="bedroom" if h % 2 else "kitchen", ts=ts)

    for d_off in (2, 1, 0):
        for h in (8, 11, 14, 17, 20):
            ts = now - d_off * 86400 - (24 - h) * 3600
            push(pet_id="maru", pet_name="Maru",
                 camera="kitchen" if h % 2 else "living_room", ts=ts)

    # Triggers the litter card.
    for i in range(15):
        push(pet_id="mochi", pet_name="Mochi", camera="litter",
             ts=now - i * 600)
    # Phantom-run cluster: 5 visits, all <90s apart, in the last hour.
    base = now - 1800
    for i in range(5):
        push(pet_id="mochi", pet_name="Mochi", camera="litter",
             ts=base + i * 50)

    # Triggers the fight detector (≥4 events, two pets, <60s window).
    base = now - 90
    for i in range(6):
        push(pet_id="mochi" if i % 2 == 0 else "maru",
             pet_name="Mochi" if i % 2 == 0 else "Maru",
             camera="living_room", ts=base + i * 8)

    # Seed one diary entry per pet so the Pet diary section is non-empty
    # in demo even without a real OpenAI key.
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="mochi", pet_name="Mochi", date=today, lang="zh-TW",
        text="今天早上四點還是巡了一圈廚房，午後就窩在臥室睡到飽。砂盆好像跑得有點勤……",
        backend="pro_relay", generated_at=now,
    ))
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="maru", pet_name="Maru", date=today, lang="zh-TW",
        text="客廳的午後陽光最好，跟 Mochi 又玩了一陣抓撓比賽，晚餐少吃了半碗。",
        backend="pro_relay", generated_at=now,
    ))

    # Sentinel last — a crash mid-seed leaves the file absent so the
    # next demo run re-seeds rather than seeing a half-populated state.
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("seeded\n", encoding="utf-8")


_seed_demo_data()


def serve() -> None:
    import uvicorn

    print("=" * 64)
    print(" pawcorder admin demo")
    print(" URL:      http://localhost:8080")
    print(" Password: demo")
    print(" Data:    ", DEMO_DIR)
    print(" Mocked:   docker, Reolink API, RTSP probes, network scan")
    print("=" * 64)
    uvicorn.run(main.app, host="127.0.0.1", port=8080, reload=False, log_level="warning")


if __name__ == "__main__":
    serve()
