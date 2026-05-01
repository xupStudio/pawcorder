"""pawcorder admin panel — FastAPI application."""
from __future__ import annotations

import asyncio
import io
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    api_keys,
    auth,
    backup as backup_mod,
    backup_schedule,
    camera_api,
    camera_compat,
    camera_setup,
    onboarding,
    cloud,
    config_store,
    docker_ops,
    embeddings,
    federated,
    health,
    heatmap,
    highlights,
    i18n,
    insights,
    line as line_api,
    marketing,
    migrations,
    nas_mount,
    network_scan,
    perf,
    pet_diary,
    pet_query,
    pets_store,
    podcast,
    platform_detect,
    privacy,
    recognition,
    recognition_backfill,
    reliability,
    telegram as tg,
    timeline,
    timelapse,
    vet_pack,
)

# Pro modules — present only when a license-paying install drops them in.
# OSS builds resolve the import to None and gate the feature off.
try:
    from .pro import pet_health  # type: ignore[attr-defined]
except ImportError:  # OSS build — health features unavailable
    pet_health = None  # type: ignore[assignment]

try:
    from .pro import recognition_backfill_pro  # type: ignore[attr-defined]
except ImportError:  # OSS build — 30-day backfill / anomaly highlights unavailable
    recognition_backfill_pro = None  # type: ignore[assignment]

try:
    from .pro import litter_monitor  # type: ignore[attr-defined]
except ImportError:
    litter_monitor = None  # type: ignore[assignment]

try:
    from .pro import fight_detector  # type: ignore[attr-defined]
except ImportError:
    fight_detector = None  # type: ignore[assignment]

try:
    from .pro import posture_detector  # type: ignore[attr-defined]
except ImportError:
    posture_detector = None  # type: ignore[assignment]

try:
    from .pro import bowl_monitor  # type: ignore[attr-defined]
except ImportError:
    bowl_monitor = None  # type: ignore[assignment]

try:
    from .pro import connect_client  # type: ignore[attr-defined]
except ImportError:
    connect_client = None  # type: ignore[assignment]

try:
    from .pro import b2b_dashboard  # type: ignore[attr-defined]
except ImportError:
    b2b_dashboard = None  # type: ignore[assignment]

from . import (
    uninstall as uninstall_mod,
    updater,
    users,
    webpush,
    weekly_health_digest,
)
from . import cameras_store
from .cameras_store import (
    Camera,
    CameraStore,
    CameraValidationError,
    validate_camera,
)

APP_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_ROOT / "templates"
STATIC_DIR = APP_ROOT / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Run schema migrations BEFORE any background task touches a YAML.
    # Soft-fails: a single bad file doesn't block the whole admin.
    try:
        for r in migrations.run_all():
            if r.applied:
                pass  # logged inside run_all()
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("pawcorder").warning("migrations failed: %s", exc)

    tg.poller.start()
    cloud.uploader.start()
    health.monitor.start()
    privacy.monitor.start()
    highlights.scheduler.start()
    if pet_health is not None:
        pet_health.monitor.start()
    if litter_monitor is not None:
        litter_monitor.monitor.start()
    if fight_detector is not None:
        fight_detector.detector.start()
    if posture_detector is not None:
        posture_detector.monitor.start()
    if bowl_monitor is not None:
        bowl_monitor.monitor.start()
    if connect_client is not None:
        connect_client.registrar.start()
    pet_diary.scheduler.start()
    federated.scheduler.start()
    podcast.scheduler.start()
    updater.checker.start()
    backup_schedule.scheduler.start()
    timelapse.scheduler.start()
    weekly_health_digest.scheduler.start()
    try:
        yield
    finally:
        await tg.poller.stop()
        await cloud.uploader.stop()
        await health.monitor.stop()
        await privacy.monitor.stop()
        await highlights.scheduler.stop()
        if pet_health is not None:
            await pet_health.monitor.stop()
        if litter_monitor is not None:
            await litter_monitor.monitor.stop()
        if fight_detector is not None:
            await fight_detector.detector.stop()
        if posture_detector is not None:
            await posture_detector.monitor.stop()
        if bowl_monitor is not None:
            await bowl_monitor.monitor.stop()
        if connect_client is not None:
            await connect_client.registrar.stop()
        await pet_diary.scheduler.stop()
        await federated.scheduler.stop()
        await podcast.scheduler.stop()
        await updater.checker.stop()
        await backup_schedule.scheduler.stop()
        await timelapse.scheduler.stop()
        await weekly_health_digest.scheduler.stop()


app = FastAPI(title="pawcorder admin", docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

camera_store = CameraStore()


# ---- helpers -------------------------------------------------------------

def _require_auth(request: Request) -> None:
    """Two acceptable credentials: session cookie (browser) OR
    Authorization: Bearer <api-key> (programmatic). Bearer auth is
    NOT subject to the CSRF header check — it's already a cross-site
    secret, and browsers can't forge custom Authorization headers
    cross-origin."""
    bearer = api_keys.from_request(request)
    if bearer is not None:
        return
    if not auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="not_authenticated")
    # CSRF: same-site=lax cookie + custom header is the belt-and-braces
    # combo recommended by OWASP for SPA-style apps. Browsers never send
    # X-Requested-With on a cross-site POST without CORS, which we don't
    # grant. See app.auth for details.
    if not auth.has_csrf_header(request):
        raise HTTPException(status_code=403, detail="csrf_header_missing")


def _require_role(request: Request, *, min_role: str) -> str:
    """Authenticate AND check role. Use min_role='family' for routes
    that family members are allowed to use, 'admin' for admin-only."""
    _require_auth(request)
    actual = users.role_from_request(request) or "admin"
    if not users.has_role(actual, min_role):
        raise HTTPException(
            status_code=403,
            detail=f"role {actual!r} cannot access this — needs {min_role!r}",
        )
    return actual


def _redirect_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def _redirect_to(target: str) -> RedirectResponse:
    return RedirectResponse(url=target, status_code=303)


def _render(name: str, request: Request, **ctx) -> HTMLResponse:
    cfg = ctx.pop("config", None) or config_store.load_config()
    cameras = ctx.pop("cameras", None)
    if cameras is None:
        cameras = camera_store.load()
    setup_complete = config_store.is_setup_complete(cfg, cameras)
    lang = i18n.get_lang_from_request(request)
    return templates.TemplateResponse(
        name,
        {
            "request": request,
            "config": cfg,
            "cameras": cameras,
            "setup_complete": setup_complete,
            "lang": lang,
            "supported_langs": i18n.SUPPORTED,
            "t": i18n.make_translator(lang),
            **ctx,
        },
    )


def _camera_from_payload(data: dict, *, fallback: Camera | None = None) -> Camera:
    def pick(key: str, default):
        v = data.get(key)
        return default if v is None or v == "" else v

    if fallback is None:
        return Camera(
            name=str(pick("name", "")).strip(),
            ip=str(pick("ip", "")).strip(),
            user=str(pick("user", "admin")).strip(),
            password=str(pick("password", "")),
            rtsp_port=int(pick("rtsp_port", 554)),
            onvif_port=int(pick("onvif_port", 8000)),
            detect_width=int(pick("detect_width", 640)),
            detect_height=int(pick("detect_height", 480)),
            enabled=bool(data.get("enabled", True)),
            connection_type=str(pick("connection_type", "unknown")),
            brand=str(pick("brand", "reolink")),
            two_way_audio=bool(data.get("two_way_audio", False)),
            audio_detection=bool(data.get("audio_detection", False)),
            zones=list(data.get("zones") or []),
            privacy_masks=list(data.get("privacy_masks") or []),
            ptz_presets=list(data.get("ptz_presets") or []),
        )
    # Update flow: keep stored values when caller didn't supply them.
    return Camera(
        name=str(pick("name", fallback.name)).strip(),
        ip=str(pick("ip", fallback.ip)).strip(),
        user=str(pick("user", fallback.user)).strip(),
        password=str(pick("password", fallback.password)),
        rtsp_port=int(pick("rtsp_port", fallback.rtsp_port)),
        onvif_port=int(pick("onvif_port", fallback.onvif_port)),
        detect_width=int(pick("detect_width", fallback.detect_width)),
        detect_height=int(pick("detect_height", fallback.detect_height)),
        enabled=bool(data.get("enabled", fallback.enabled)),
        connection_type=str(pick("connection_type", fallback.connection_type)),
        brand=str(pick("brand", fallback.brand)),
        two_way_audio=bool(data.get("two_way_audio", fallback.two_way_audio)),
        audio_detection=bool(data.get("audio_detection", fallback.audio_detection)),
        zones=list(data.get("zones")) if data.get("zones") is not None else fallback.zones,
        privacy_masks=list(data.get("privacy_masks")) if data.get("privacy_masks") is not None else fallback.privacy_masks,
        ptz_presets=list(data.get("ptz_presets")) if data.get("ptz_presets") is not None else fallback.ptz_presets,
    )


def _ensure_secrets(cfg: config_store.Config) -> config_store.Config:
    changed = False
    if not cfg.frigate_rtsp_password:
        cfg.frigate_rtsp_password = config_store.random_password(20)
        changed = True
    if not cfg.admin_session_secret:
        cfg.admin_session_secret = config_store.random_secret()
        changed = True
    if changed:
        config_store.save_config(cfg)
    return cfg


# Brands rendered with a step-by-step in-app setup panel on the cameras
# page. Anything not in this set gets an empty panel payload — they're
# either fully automatic (Reolink, Hikvision, …) or unrecognised.
_PANEL_BRANDS: frozenset[str] = frozenset({"tapo", "imou", "wyze", "ubiquiti", "other"})

# Defensive cap on the per-brand step count; the most we ship today is 4.
_MAX_BRAND_SETUP_STEPS = 10


def _attach_setup_panel(brand: dict, t) -> None:
    """Add `setup_title` + `setup_steps` to a brand dict for the cameras-page panel.

    The translator `t` is bound to the request's locale; we look up
    `BRAND_SETUP_<KEY>_TITLE` and `_STEP_N` until the key is missing
    (capped by `_MAX_BRAND_SETUP_STEPS`). Brands outside `_PANEL_BRANDS`
    get an empty payload so the front-end can branch uniformly on
    `setup_title`'s truthiness.
    """
    if brand["key"] not in _PANEL_BRANDS:
        brand["setup_title"] = ""
        brand["setup_steps"] = []
        return
    upper = brand["key"].upper()
    title_key = f"BRAND_SETUP_{upper}_TITLE"
    title = t(title_key)
    brand["setup_title"] = title if title != title_key else ""
    steps: list[str] = []
    for n in range(1, _MAX_BRAND_SETUP_STEPS + 1):
        step_key = f"BRAND_SETUP_{upper}_STEP_{n}"
        translated = t(step_key)
        if translated == step_key:
            break
        steps.append(translated)
    brand["setup_steps"] = steps


async def _best_effort_connection_type(camera: Camera) -> str:
    """Best-effort Wi-Fi vs Wired classification. Routes via the brand-aware
    dispatcher; only Reolink + Hikvision + Dahua/Amcrest currently report
    a usable link type. Never raises."""
    # Manual-setup brands (Tapo / Imou / Wyze) intentionally short-circuit
    # to a sentinel — no point round-tripping the dispatcher every save.
    if camera_setup.is_manual_brand(camera.brand):
        return "unknown"
    try:
        result = await camera_setup.auto_configure_for_brand(
            camera.brand, camera.ip, camera.user, camera.password,
        )
        return result.get("connection_type", "unknown")
    except Exception:  # noqa: BLE001 - informational, fall back silently
        return "unknown"


def _rerender_and_restart(silent: bool = True) -> None:
    """Re-render frigate.yml and restart Frigate, ignoring restart errors."""
    cfg = config_store.load_config()
    cameras = camera_store.load()
    if not cameras:
        return
    config_store.write_frigate_config(cfg, cameras)
    try:
        docker_ops.restart_frigate()
    except RuntimeError:
        if not silent:
            raise


# ---- auth ----------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    if auth.is_authenticated(request):
        return _redirect_to("/")
    lang = i18n.get_lang_from_request(request)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": error,
            "multi_user": users.has_users(),
            "lang": lang,
            "supported_langs": i18n.SUPPORTED,
            "t": i18n.make_translator(lang),
        },
    )


@app.post("/api/lang")
async def api_lang(payload: dict):
    lang = (payload.get("lang") or "").strip()
    if lang not in i18n.SUPPORTED:
        raise HTTPException(status_code=400, detail="unsupported language")
    response = JSONResponse({"ok": True, "lang": lang})
    response.set_cookie(
        i18n.LANG_COOKIE, lang,
        max_age=60 * 60 * 24 * 365, httponly=False, samesite="lax", path="/",
    )
    return response


@app.post("/login")
async def login_submit(password: str = Form(...), username: str = Form("")):
    """Two paths:
       1. Multi-user (users.yml exists) — username + password go
          through users.authenticate, role baked into the session.
       2. Legacy (no users.yml) — single password matches
          ADMIN_PASSWORD. Session marked as 'admin'.
    """
    if users.has_users():
        # Multi-user path: username required.
        if not username:
            return _redirect_to("/login?error=invalid")
        user = users.authenticate(username.strip(), password)
        if not user:
            return _redirect_to("/login?error=invalid")
        token = auth.issue_session(username=user.username, role=user.role)
    else:
        if not auth.password_matches(password):
            return _redirect_to("/login?error=invalid")
        token = auth.issue_session()  # legacy — implicit admin
    response = _redirect_to("/")
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.SESSION_MAX_AGE_SECONDS,
        httponly=True, samesite="lax", path="/",
    )
    return response


@app.post("/logout")
async def logout():
    response = _redirect_to("/login")
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


# ---- pages ---------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    cfg = config_store.load_config()
    cameras = camera_store.load()
    if not config_store.is_setup_complete(cfg, cameras):
        return _redirect_to("/setup")
    statuses = {"frigate": docker_ops.get_frigate_status()}
    return _render("dashboard.html", request, config=cfg, cameras=cameras, frigate_status=statuses["frigate"])


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("setup.html", request)


@app.get("/cameras", response_class=HTMLResponse)
async def cameras_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("cameras.html", request)


@app.get("/detection", response_class=HTMLResponse)
async def detection_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("detection.html", request)


@app.get("/storage", response_class=HTMLResponse)
async def storage_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("storage.html", request)


@app.post("/api/storage/test-mount")
async def api_storage_test_mount(request: Request, payload: dict):
    """Try mounting the NAS in a tmp dir without persisting. Surfaces
    auth / share-path errors before the user commits to fstab."""
    _require_role(request, min_role="admin")
    cfg = nas_mount.MountConfig(
        protocol=str(payload.get("protocol") or "").strip().lower(),
        server=str(payload.get("server") or "").strip(),
        share=str(payload.get("share") or "").strip(),
        mount_point=str(payload.get("mount_point") or "/mnt/pawcorder").strip(),
        username=str(payload.get("username") or ""),
        password=str(payload.get("password") or ""),
    )
    result = await nas_mount.test_mount(cfg)
    return {"ok": result.ok, "message": result.message, "output": result.output}


@app.post("/api/storage/install-mount")
async def api_storage_install_mount(request: Request, payload: dict):
    """Persist the mount — append fstab entry + mount immediately.
    Re-running replaces the previous pawcorder-managed lines."""
    _require_role(request, min_role="admin")
    cfg = nas_mount.MountConfig(
        protocol=str(payload.get("protocol") or "").strip().lower(),
        server=str(payload.get("server") or "").strip(),
        share=str(payload.get("share") or "").strip(),
        mount_point=str(payload.get("mount_point") or "/mnt/pawcorder").strip(),
        username=str(payload.get("username") or ""),
        password=str(payload.get("password") or ""),
    )
    err = nas_mount.install_to_fstab(cfg)
    if err:
        raise HTTPException(status_code=400, detail=err)
    ok, output = await nas_mount.mount_now(cfg.mount_point)
    if not ok:
        # fstab is updated but mount-now failed — common when host runs
        # in Docker without privileged mode. Tell the user.
        raise HTTPException(
            status_code=502,
            detail=f"fstab updated but mount failed (will mount on next reboot): {output}",
        )
    # Save STORAGE_PATH so Frigate / admin write recordings here next time.
    cfg_obj = config_store.load_config()
    cfg_obj.storage_path = cfg.mount_point
    config_store.save_config(cfg_obj)
    return {"ok": True, "mount_point": cfg.mount_point}


@app.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    status = docker_ops.get_frigate_status()
    logs = docker_ops.recent_frigate_logs(tail=200)
    from . import pet_health_overview
    return _render(
        "system.html", request,
        frigate_status=status, frigate_logs=logs,
        uptime_svg=pet_health_overview.system_uptime_ribbon(days=7),
    )


@app.get("/mobile", response_class=HTMLResponse)
async def mobile_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    host = request.url.hostname or "localhost"
    return _render(
        "mobile.html",
        request,
        lan_host=host,
        lan_admin_url=f"http://{host}:8080",
        lan_frigate_url=f"http://{host}:5000",
    )


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("notifications.html", request)


@app.get("/home-assistant", response_class=HTMLResponse)
async def home_assistant_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("home_assistant.html", request)


@app.get("/cloud", response_class=HTMLResponse)
async def cloud_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render(
        "cloud.html", request,
        cloud_remotes=cloud.list_remotes(),
        supported_backends=list(cloud.SUPPORTED_BACKENDS),
    )


@app.post("/api/cloud/remote")
async def api_cloud_remote_save(request: Request, payload: dict):
    _require_auth(request)
    name = (payload.get("name") or "").strip()
    backend = (payload.get("backend") or "").strip()
    if not name or backend not in cloud.SUPPORTED_BACKENDS:
        raise HTTPException(status_code=400, detail="name and supported backend required")
    try:
        fields = cloud.fields_for_backend(backend, payload.get("fields") or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # If editing and the new payload has no token (Drive/Dropbox/OneDrive),
    # keep the old one rather than blanking it out.
    if backend in ("drive", "dropbox", "onedrive") and not fields.get("token"):
        existing = cloud.get_remote(name)
        if existing.get("token"):
            fields["token"] = existing["token"]
    cloud.save_remote(name, fields)
    cfg = config_store.load_config()
    cfg.cloud_backend = backend
    cfg.cloud_remote_name = name
    config_store.save_config(cfg)
    return {"ok": True, "name": name}


@app.delete("/api/cloud/remote/{name}")
async def api_cloud_remote_delete(request: Request, name: str):
    _require_auth(request)
    cloud.delete_remote(name)
    return {"ok": True}


@app.post("/api/cloud/test")
async def api_cloud_test(request: Request, payload: dict):
    _require_auth(request)
    name = (payload.get("name") or "").strip() or config_store.load_config().cloud_remote_name
    result = await cloud.test_remote(name)
    if not result.ok:
        return JSONResponse(status_code=400, content={"ok": False, "error": result.detail})
    return {"ok": True, "detail": result.detail}


@app.get("/api/cloud/remotes")
async def api_cloud_remotes(request: Request):
    _require_auth(request)
    return {"remotes": cloud.list_remotes()}


@app.get("/api/cloud/quota")
async def api_cloud_quota(request: Request, name: str | None = None):
    """Reports total / free / used GB on the configured cloud, plus how much
    of it pawcorder is using. Used by the /cloud page to show a usage bar
    and recommend an adaptive size cap."""
    _require_auth(request)
    cfg = config_store.load_config()
    target = (name or cfg.cloud_remote_name).strip()
    if not target or target not in cloud.list_remotes():
        raise HTTPException(status_code=400, detail="no cloud remote configured")
    quota = await cloud.get_quota(target, cfg.cloud_remote_path)
    recommended_cap_bytes = (
        cloud.estimate_max_for_free_space(quota.free_bytes + quota.pawcorder_bytes)
        if quota.quota_supported else 0
    )
    return {
        "quota_supported": quota.quota_supported,
        "total_bytes": quota.total_bytes,
        "used_bytes": quota.used_bytes,
        "free_bytes": quota.free_bytes,
        "pawcorder_bytes": quota.pawcorder_bytes,
        "recommended_cap_bytes": recommended_cap_bytes,
    }


@app.get("/hardware", response_class=HTMLResponse)
async def hardware_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    info = platform_detect.detect()
    recommended = platform_detect.recommended_detector(info)
    return _render(
        "hardware.html", request,
        platform_info=info.to_dict(),
        platform_vendor=platform_detect.vendor_label(info.cpu_vendor),
        recommended_detector=recommended,
        valid_detectors=list(platform_detect.VALID_DETECTORS),
    )


@app.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("backup.html", request, app_version=updater.current_version())


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    state = privacy.load_state()
    return _render("privacy.html", request, privacy_state=state.to_dict())


# ---- JSON API ------------------------------------------------------------

@app.get("/api/status")
async def api_status(request: Request):
    _require_auth(request)
    cfg = config_store.load_config()
    cameras = camera_store.load()
    status = docker_ops.get_frigate_status()
    return {
        "setup_complete": config_store.is_setup_complete(cfg, cameras),
        "camera_count": len(cameras),
        "cameras": [
            {"name": c.name, "ip": c.ip, "enabled": c.enabled, "connection_type": c.connection_type}
            for c in cameras
        ],
        "frigate": {
            "exists": status.exists,
            "running": status.running,
            "status": status.status,
            "health": status.health,
            "image": status.image,
        },
    }


@app.get("/api/onboarding")
async def api_onboarding(request: Request):
    """Return the dashboard onboarding-widget payload (derived state).

    The translator argument lets `onboarding.get_state` pre-populate
    each step's `title` and `why` strings so the dashboard template
    doesn't need its own per-step lookup table.
    """
    _require_auth(request)
    cfg = config_store.load_config()
    pets = pets_store.PetStore().load()
    privacy_state = privacy.load_state()
    t = i18n.make_translator(i18n.get_lang_from_request(request))
    return onboarding.get_state(cfg, pets, privacy_state, translator=t)


@app.post("/api/onboarding/skip")
async def api_onboarding_skip(request: Request, payload: dict):
    """Hide one step (`{"step": "key"}`) or all (`{"all": true}`) from the widget."""
    _require_auth(request)
    if payload.get("all"):
        onboarding.skip_all()
        return {"ok": True}
    step = (payload.get("step") or "").strip()
    if not step:
        raise HTTPException(status_code=400, detail="step or all=true required")
    try:
        onboarding.skip_step(step)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/onboarding/reset")
async def api_onboarding_reset(request: Request):
    """Wipe the skip set — the dashboard widget reappears with every
    step pending again. Backs the "Reset tutorial" button on /tutorial."""
    _require_auth(request)
    onboarding.reset()
    return {"ok": True}


@app.get("/tutorial", response_class=HTMLResponse)
async def tutorial_page(request: Request):
    """Always-accessible tutorial — same checklist as the dashboard
    widget, but visible even after `all_done` and with a reset button.
    Useful when a user wants to re-walk the steps after a config wipe,
    a new family member, or just to remember which features are off."""
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("tutorial.html", request)


@app.post("/api/scan")
async def api_scan(request: Request, payload: dict):
    _require_auth(request)
    cidr = (payload.get("cidr") or "").strip()
    if not cidr:
        # Empty cidr → auto-detect the host's LAN /24. Saves users from
        # typing CIDR notation in the wizard — they put the camera on
        # the same Wi-Fi and tap the button.
        try:
            cidr = network_scan.detect_local_subnet()
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"could not auto-detect local subnet: {exc}",
            ) from exc
    try:
        candidates = await network_scan.scan_for_cameras(cidr)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"candidates": [c.__dict__ for c in candidates], "cidr": cidr}


# ---- cameras CRUD --------------------------------------------------------

@app.get("/api/cameras")
async def api_cameras_list(request: Request):
    _require_auth(request)
    return {"cameras": [c.to_dict() for c in camera_store.load()]}


@app.get("/api/camera-brands")
async def api_camera_brands(request: Request):
    """Return the brand compatibility matrix used by the cameras page.

    The cameras page renders a step-by-step setup panel for manual brands
    (and the "other" catch-all). Rather than hard-code per-brand `<template>`
    blocks in the HTML, we attach the translated `setup_title` + `setup_steps`
    here so the template can iterate them with a single `x-for`. Adding a new
    manual brand is then a Python-only change: BrandSpec + i18n keys.
    """
    _require_auth(request)
    t = i18n.make_translator(i18n.get_lang_from_request(request))
    brands = camera_compat.list_brands()
    for brand in brands:
        _attach_setup_panel(brand, t)
    return {"brands": brands}


@app.post("/api/cameras")
async def api_cameras_create(request: Request, payload: dict):
    _require_auth(request)
    camera = _camera_from_payload(payload)
    if camera.connection_type == "unknown":
        camera.connection_type = await _best_effort_connection_type(camera)
    try:
        camera_store.create(camera)
    except CameraValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _ensure_secrets(config_store.load_config())
    _rerender_and_restart()
    return {"ok": True, "camera": camera.to_dict()}


@app.get("/api/cameras/{name}")
async def api_cameras_get(request: Request, name: str):
    _require_auth(request)
    c = camera_store.get(name)
    if not c:
        raise HTTPException(status_code=404, detail="camera not found")
    return {"camera": c.to_dict()}


@app.put("/api/cameras/{name}")
async def api_cameras_update(request: Request, name: str, payload: dict):
    _require_auth(request)
    existing = camera_store.get(name)
    if not existing:
        raise HTTPException(status_code=404, detail="camera not found")
    updated = _camera_from_payload(payload, fallback=existing)
    # If IP/credentials changed and the caller didn't pass a fresh type, re-detect.
    creds_changed = (
        updated.ip != existing.ip
        or updated.user != existing.user
        or updated.password != existing.password
    )
    payload_set_type = isinstance(payload.get("connection_type"), str) and payload["connection_type"]
    if creds_changed and not payload_set_type:
        updated.connection_type = await _best_effort_connection_type(updated)
    try:
        camera_store.update(name, updated)
    except CameraValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="camera not found")
    _rerender_and_restart()
    return {"ok": True, "camera": updated.to_dict()}


@app.delete("/api/cameras/{name}")
async def api_cameras_delete(request: Request, name: str):
    _require_auth(request)
    if not camera_store.delete(name):
        raise HTTPException(status_code=404, detail="camera not found")
    _rerender_and_restart()
    return {"ok": True}


@app.post("/api/cameras/{name}/test")
async def api_camera_test_named(request: Request, name: str, payload: dict | None = None):
    """Test a camera that already exists in cameras.yml."""
    _require_auth(request)
    c = camera_store.get(name)
    if not c:
        raise HTTPException(status_code=404, detail="camera not found")
    return await _run_camera_test(
        ip=c.ip, user=c.user, password=c.password, port=c.rtsp_port,
        brand=c.brand,
        auto_enable_rtsp=bool((payload or {}).get("auto_enable_rtsp", True)),
    )


@app.post("/api/cameras/test")
async def api_camera_test_new(request: Request, payload: dict):
    """Test arbitrary credentials before saving (used by the setup wizard)."""
    _require_auth(request)
    ip = (payload.get("ip") or "").strip()
    user = (payload.get("user") or "admin").strip()
    password = payload.get("password") or ""
    port = int(payload.get("rtsp_port") or payload.get("port") or 554)
    auto_enable_rtsp = bool(payload.get("auto_enable_rtsp", True))
    brand = (payload.get("brand") or "reolink").strip() or "reolink"
    if not ip or not password:
        raise HTTPException(status_code=400, detail="ip and password are required")
    return await _run_camera_test(
        ip=ip, user=user, password=password, port=port,
        brand=brand, auto_enable_rtsp=auto_enable_rtsp,
    )


async def _run_camera_test(
    *, ip: str, user: str, password: str, port: int, brand: str, auto_enable_rtsp: bool,
) -> dict:
    """Brand-aware camera connectivity test for the setup wizard.

    Steps:
      1. If `auto_enable_rtsp` and brand has an automatable handler, call
         `camera_setup.auto_configure_for_brand` to read device info, toggle
         RTSP on (where applicable), and pick up brand-specific RTSP URLs.
      2. Probe the main + sub RTSP streams via ffprobe.

    The response key `reolink_login` is preserved for UI back-compat — it now
    carries the result of whatever brand-specific handler ran (or the manual
    sentinel for Tapo/Imou/Wyze/other).
    """
    response: dict = {
        "reolink_login": None,
        "rtsp_main": None,
        "rtsp_sub": None,
        "connection_type": "unknown",
        "brand": brand,
    }
    main_url: str | None = None
    sub_url: str | None = None
    if auto_enable_rtsp:
        try:
            result = await camera_setup.auto_configure_for_brand(brand, ip, user, password)
            response["reolink_login"] = {"ok": True, "device": result.get("device"), "manual": result.get("manual", False)}
            response["connection_type"] = result.get("connection_type", "unknown")
            # If the brand handler returned RTSP URLs (everything except the
            # manual sentinel), use those — they're built with each brand's
            # native path layout.
            if result.get("rtsp_main"):
                main_url = result["rtsp_main"]
            if result.get("rtsp_sub"):
                sub_url = result["rtsp_sub"]
        except PermissionError as exc:
            response["reolink_login"] = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface every failure to UI
            response["reolink_login"] = {"ok": False, "error": str(exc)}
    # Fall back to the brand-specific URL builder when the handler didn't
    # supply URLs (Reolink module sometimes returns just device info; manual
    # brands rely on the user-supplied brand template). Using the brand
    # template here matters: the previous code hard-coded Reolink's
    # h264Preview path, which guaranteed a probe failure when testing a
    # non-Reolink IP.
    if main_url is None:
        main_url = camera_compat.build_rtsp_url(brand, ip, user, password, port=port, sub=False)
    if sub_url is None:
        sub_url = camera_compat.build_rtsp_url(brand, ip, user, password, port=port, sub=True)
    # ffprobe each RTSP URL in parallel — they're independent network reads
    # and each carries an 8s timeout, so serial worst-case is ~16s vs ~8s
    # parallel for the wizard's "Test camera" button.
    main_probe, sub_probe = await asyncio.gather(
        camera_api.probe_rtsp(main_url),
        camera_api.probe_rtsp(sub_url),
    )
    response["rtsp_main"] = main_probe.__dict__
    response["rtsp_sub"] = sub_probe.__dict__
    response["ok"] = main_probe.ok and sub_probe.ok
    return response


# ---- host config ---------------------------------------------------------

@app.post("/api/config/save")
async def api_config_save(request: Request, payload: dict):
    _require_auth(request)
    cfg = config_store.load_config()
    section = payload.get("section")
    data = payload.get("data") or {}

    if section == "storage":
        cfg.storage_path = (data.get("storage_path") or cfg.storage_path).strip()
    elif section == "detection":
        cfg.pet_min_score = str(data.get("pet_min_score") or cfg.pet_min_score)
        cfg.pet_threshold = str(data.get("pet_threshold") or cfg.pet_threshold)
        if "track_cat" in data:
            cfg.track_cat = bool(data.get("track_cat"))
        if "track_dog" in data:
            cfg.track_dog = bool(data.get("track_dog"))
        if "track_person" in data:
            cfg.track_person = bool(data.get("track_person"))
        if not (cfg.track_cat or cfg.track_dog or cfg.track_person):
            raise HTTPException(status_code=400, detail="at least one species must be tracked")
    elif section == "general":
        cfg.tz = (data.get("tz") or cfg.tz).strip()
    elif section == "tailscale":
        cfg.tailscale_hostname = (data.get("tailscale_hostname") or "").strip()
    elif section == "telegram":
        if "telegram_bot_token" in data:
            cfg.telegram_bot_token = (data.get("telegram_bot_token") or "").strip()
        if "telegram_chat_id" in data:
            cfg.telegram_chat_id = (data.get("telegram_chat_id") or "").strip()
        if "telegram_enabled" in data:
            cfg.telegram_enabled = bool(data.get("telegram_enabled"))
    elif section == "line":
        if "line_channel_token" in data:
            cfg.line_channel_token = (data.get("line_channel_token") or "").strip()
        if "line_target_id" in data:
            cfg.line_target_id = (data.get("line_target_id") or "").strip()
        if "line_enabled" in data:
            cfg.line_enabled = bool(data.get("line_enabled"))
    elif section == "hardware":
        detector = (data.get("detector_type") or "").strip()
        if detector and detector not in platform_detect.VALID_DETECTORS:
            raise HTTPException(status_code=400, detail=f"unknown detector_type {detector!r}")
        if detector:
            cfg.detector_type = detector
    elif section == "cloud":
        if "cloud_enabled" in data:
            cfg.cloud_enabled = bool(data.get("cloud_enabled"))
        if "cloud_backend" in data:
            backend = (data.get("cloud_backend") or "").strip()
            if backend and backend not in cloud.SUPPORTED_BACKENDS:
                raise HTTPException(status_code=400, detail=f"unsupported backend {backend!r}")
            cfg.cloud_backend = backend
        for key in ("cloud_remote_name", "cloud_remote_path"):
            if key in data:
                setattr(cfg, key, (data.get(key) or "").strip() or getattr(cfg, key))
        if "cloud_upload_only_pets" in data:
            cfg.cloud_upload_only_pets = bool(data.get("cloud_upload_only_pets"))
        if "cloud_upload_min_score" in data:
            cfg.cloud_upload_min_score = str(data.get("cloud_upload_min_score") or cfg.cloud_upload_min_score)
        if "cloud_retention_days" in data:
            cfg.cloud_retention_days = str(data.get("cloud_retention_days") or cfg.cloud_retention_days)
        if "cloud_max_size_gb" in data:
            cfg.cloud_max_size_gb = str(data.get("cloud_max_size_gb") or "0")
        if "cloud_size_mode" in data:
            mode = (data.get("cloud_size_mode") or "manual").strip()
            if mode not in ("manual", "adaptive"):
                raise HTTPException(status_code=400, detail=f"unknown size mode {mode!r}")
            cfg.cloud_size_mode = mode
        if "cloud_adaptive_fraction" in data:
            try:
                frac = float(data.get("cloud_adaptive_fraction"))
                if not 0.1 <= frac <= 0.95:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="cloud_adaptive_fraction must be between 0.1 and 0.95")
            cfg.cloud_adaptive_fraction = str(frac)
    else:
        raise HTTPException(status_code=400, detail=f"unknown section {section!r}")

    cfg = _ensure_secrets(cfg)
    config_store.save_config(cfg)
    config_store.render_and_write_if_complete(cfg)
    return {"ok": True, "setup_complete": config_store.is_setup_complete(cfg, camera_store.load())}


@app.post("/api/admin/password")
async def api_admin_password(request: Request, payload: dict):
    _require_auth(request)
    new_password = payload.get("new_password") or ""
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    cfg = config_store.load_config()
    cfg.admin_password = new_password
    config_store.save_config(cfg)
    return {"ok": True}


@app.get("/api/system/ai-tokens")
async def api_system_ai_tokens(request: Request):
    """Return whether the OpenAI key / Pro license / Ollama URL are
    set, NOT the values.

    The UI uses the booleans to render placeholder dots — exposing the
    raw secret over the API would let a leaked session lift it. The
    Ollama URL itself isn't a secret (it's almost always a localhost-y
    address) so we echo it back so the user can see what they configured.

    Pro license payload (subject id, expiry, days_left) is decoded
    locally — `verify_license` doesn't talk to the relay, so failed
    networks don't break this view. Revocation status only shows up
    on next live diary call returning 401.
    """
    _require_auth(request)
    cfg = config_store.load_config()
    license_info: dict = {}
    if cfg.pawcorder_pro_license_key:
        try:
            # Local stdlib import to avoid pulling relay deps into OSS
            # builds. The verify path uses HMAC + the LICENSE_SECRET,
            # which the admin doesn't have — but the payload is
            # base64-decodable without the secret, and we only need
            # the exp claim for the UI.
            import base64 as _b64, json as _j
            token = cfg.pawcorder_pro_license_key.strip()
            if token.startswith("pro_") and "." in token:
                body_b64 = token[4:].split(".", 1)[0]
                pad = (-len(body_b64)) % 4
                body = _b64.urlsafe_b64decode(body_b64 + ("=" * pad))
                claims = _j.loads(body)
                exp = int(claims.get("exp") or 0)
                import time as _t
                now = int(_t.time())
                license_info = {
                    "sub": str(claims.get("sub") or ""),
                    "exp": exp,
                    "tier": str(claims.get("tier") or "pro"),
                    "days_left": max(0, (exp - now) // 86400),
                    "expired": exp <= now,
                }
        except (ValueError, KeyError, TypeError):
            license_info = {"malformed": True}
    from . import embeddings, reenroll
    # Map the registry key to the same plain-language label the
    # System-page dropdown uses, so the "Currently running:" hint
    # doesn't leak engineer jargon ("mobilenetv3_small_100") back to
    # owners after the dropdown was already translated.
    active_name = embeddings.active_backbone_name()
    lang = i18n.get_lang_from_request(request)
    backbone_display_keys = {
        "mobilenetv3_small_100": "SYS_RECOG_BACKBONE_MOBILENET",
        "dinov2_small": "SYS_RECOG_BACKBONE_DINOV2",
    }
    active_display = (
        i18n.t(backbone_display_keys[active_name], lang=lang)
        if active_name in backbone_display_keys else active_name
    )
    return {
        "has_openai_key":     bool(cfg.openai_api_key),
        "has_pro_license":    bool(cfg.pawcorder_pro_license_key),
        "has_gemini_key":     bool(getattr(cfg, "gemini_api_key", "")),
        "has_anthropic_key":  bool(getattr(cfg, "anthropic_api_key", "")),
        "ollama_base_url":    cfg.ollama_base_url,
        "ollama_model":       cfg.ollama_model,
        "llm_provider_preference": getattr(cfg, "llm_provider_preference", "auto"),
        "tts_provider_preference": getattr(cfg, "tts_provider_preference", "auto"),
        "tts_voice":          getattr(cfg, "tts_voice", ""),
        "embedding_backbone": getattr(cfg, "embedding_backbone", ""),
        "conformal_sensitivity": getattr(cfg, "conformal_sensitivity", "0.10"),
        "active_backbone":    active_display,
        "recognition_backbones": embeddings.supported_backbones(),
        # Stale = photos whose stored backbone doesn't match the active
        # one. /pets page also surfaces this so the alert chases the
        # operator wherever they look.
        "recognition_stale_count": reenroll.stale_count(),
        "active_backend":     pet_diary.active_backend(cfg),
        "license":            license_info,
    }


_SECRET_MAX_LEN = 256
_SECRET_FORBIDDEN_CHARS = {"\n", "\r", "\x00", "\"", "'"}


def _validated_secret(value: str) -> str:
    """Reject obviously corrupting characters before we round-trip
    a secret through .env. Newlines would split the env line into two,
    quotes would close the value early, NULs would truncate at parse.
    Length cap is defensive against huge accidental pastes."""
    if any(ch in value for ch in _SECRET_FORBIDDEN_CHARS):
        raise HTTPException(
            status_code=400,
            detail="key contains forbidden characters (newlines, NUL, or quotes)",
        )
    if len(value) > _SECRET_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"key exceeds {_SECRET_MAX_LEN}-character limit",
        )
    return value


@app.post("/api/system/ai-tokens")
async def api_system_ai_tokens_update(request: Request, payload: dict):
    """Persist OpenAI key and/or pawcorder Pro license.

    Field semantics distinguish:
      - missing from payload → unchanged (UI sends only what the user touched)
      - present and non-empty → set to that value (validated)
      - present and explicitly empty string → clear the stored secret
        (e.g., the user is rotating a leaked key or downgrading from Pro)

    The UI's "••••••••" placeholder is filtered client-side, so the
    server never needs to special-case dots.
    """
    _require_auth(request)
    cfg = config_store.load_config()
    if "openai_api_key" in payload:
        raw = payload["openai_api_key"]
        if raw is None or raw == "":
            cfg.openai_api_key = ""    # explicit clear
        else:
            cfg.openai_api_key = _validated_secret(raw.strip())
    if "pawcorder_pro_license_key" in payload:
        raw = payload["pawcorder_pro_license_key"]
        if raw is None or raw == "":
            cfg.pawcorder_pro_license_key = ""
        else:
            cfg.pawcorder_pro_license_key = _validated_secret(raw.strip())
    if "ollama_base_url" in payload:
        raw = payload["ollama_base_url"]
        if raw is None or raw == "":
            cfg.ollama_base_url = ""
        else:
            url = raw.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                raise HTTPException(
                    status_code=400,
                    detail="ollama_base_url must be an http:// or https:// URL",
                )
            cfg.ollama_base_url = _validated_secret(url)
    if "ollama_model" in payload:
        raw = (payload.get("ollama_model") or "").strip() or "qwen2.5:3b"
        cfg.ollama_model = _validated_secret(raw)
    if "gemini_api_key" in payload:
        raw = payload["gemini_api_key"]
        if raw is None or raw == "":
            cfg.gemini_api_key = ""
        else:
            cfg.gemini_api_key = _validated_secret(raw.strip())
    if "anthropic_api_key" in payload:
        raw = payload["anthropic_api_key"]
        if raw is None or raw == "":
            cfg.anthropic_api_key = ""
        else:
            cfg.anthropic_api_key = _validated_secret(raw.strip())
    if "llm_provider_preference" in payload:
        raw = (payload.get("llm_provider_preference") or "auto").strip().lower()
        # Whitelist — anything outside this set goes to disk and could
        # silently never match an active backend.
        if raw not in ("auto", "ollama", "openai", "gemini",
                        "anthropic", "pro_relay"):
            raise HTTPException(status_code=400,
                                 detail="invalid_llm_provider_preference")
        cfg.llm_provider_preference = raw
    if "tts_provider_preference" in payload:
        raw = (payload.get("tts_provider_preference") or "auto").strip().lower()
        if raw not in ("auto", "openai", "cartesia", "elevenlabs", "xtts"):
            raise HTTPException(status_code=400,
                                 detail="invalid_tts_provider_preference")
        cfg.tts_provider_preference = raw
    if "tts_voice" in payload:
        raw = (payload.get("tts_voice") or "").strip()
        # tts_voice isn't a secret (it's a vendor voice ID alias) but
        # we still validate length / forbidden chars to keep the .env
        # round-trip safe.
        cfg.tts_voice = _validated_secret(raw) if raw else ""
    if "embedding_backbone" in payload:
        raw = (payload.get("embedding_backbone") or "").strip()
        from . import embeddings as _emb
        valid = {b["name"] for b in _emb.supported_backbones()}
        if raw and raw not in valid:
            raise HTTPException(status_code=400,
                                 detail="invalid_embedding_backbone")
        cfg.embedding_backbone = raw
    if "conformal_sensitivity" in payload:
        try:
            v = float(payload.get("conformal_sensitivity") or 0.10)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                 detail="invalid_conformal_sensitivity")
        # Clamp into the slider's published range — both ends. 0.30
        # ceiling keeps an API caller from configuring a value that
        # would chip on half their days.
        v = max(0.01, min(0.30, v))
        cfg.conformal_sensitivity = f"{v:.2f}"
    config_store.save_config(cfg)
    if "embedding_backbone" in payload:
        # Save just rewrote .env; the running process still has the old
        # PAWCORDER_EMBEDDING_BACKBONE in os.environ. Push the new value
        # in and refresh derived module state so the in-process
        # active_backbone_name(), EMBEDDING_DIM, and the extractor
        # singleton all see the new pick — without this the operator
        # would have to restart the admin before re-enroll could do
        # anything useful.
        os.environ["PAWCORDER_EMBEDDING_BACKBONE"] = cfg.embedding_backbone
        from . import embeddings as _emb
        _emb.refresh_active()
        # Pre-warm the model download in a background thread. The first
        # call to ``extractor.load()`` after a backbone swap pays an
        # ~80 MB urllib pull (DINOv2-small is the heavy one); doing it
        # here while the operator reads the help text means the
        # re-enroll click later doesn't hit a reverse-proxy timeout.
        # Soft-fails — a missing network just defers the cost; never
        # fails the save.
        import threading as _t
        def _warm():
            try:
                _emb.download_model()
            except Exception:  # noqa: BLE001
                pass
        _t.Thread(target=_warm, name="warm-embedding-model",
                   daemon=True).start()
    return {"ok": True}


@app.post("/api/pets/reenroll")
async def api_pets_reenroll(request: Request):
    """Re-embed every reference photo against the currently active
    backbone. Synchronous — the route blocks until done. See
    ``app.reenroll`` for the loop and the per-photo failure handling.
    """
    _require_auth(request)
    from . import reenroll
    result = reenroll.reenroll_all()
    return result.to_dict()


@app.get("/api/system/federated")
async def api_system_federated(request: Request):
    """Status of the federated baseline feature: opt-in flag + last
    cohort fetched + whether a Pro license is configured.

    The page UI uses this to render the toggle, the consent text, and
    the "we last submitted N days ago" hint."""
    _require_auth(request)
    cfg = config_store.load_config()
    cohorts = federated.read_cached_cohorts()
    return {
        "opt_in": cfg.federated_opt_in,
        "license_present": bool(cfg.pawcorder_pro_license_key),
        "cohorts": cohorts,
    }


@app.post("/api/system/federated")
async def api_system_federated_update(request: Request, payload: dict):
    """Persist the opt-in flag. The relay does the actual submission
    on its own schedule — flipping this is just consent."""
    _require_auth(request)
    cfg = config_store.load_config()
    raw = payload.get("opt_in")
    if isinstance(raw, bool):
        cfg.federated_opt_in = raw
    elif isinstance(raw, str):
        cfg.federated_opt_in = raw in ("1", "true", "True", "on")
    else:
        raise HTTPException(status_code=400, detail="opt_in must be boolean")
    config_store.save_config(cfg)
    return {"ok": True, "opt_in": cfg.federated_opt_in}


@app.get("/api/system/health-detectors")
async def api_system_health_detectors(request: Request):
    """Pro health-detector knobs. Lists enabled cameras with a per-camera
    health summary so the UI can render BOTH the dropdown for the
    litter-camera setting AND the per-camera status rows in the system
    health panel from one fetch.

    Each camera entry has the shape ``{name, ok, message}`` — `ok` is
    true when the camera is enabled and (best-effort) reachable; the
    message is a short user-facing line."""
    _require_auth(request)
    cfg = config_store.load_config()
    cams = [
        {
            "name": c.name,
            "ok": bool(c.enabled),
            "message": (
                f"{c.connection_type or 'wired'} · {c.ip}"
                if c.enabled else "disabled"
            ),
        }
        for c in camera_store.load() if c.enabled
    ]
    return {
        "available": litter_monitor is not None,
        "litter_box_camera": cfg.litter_box_camera,
        "litter_visits_alert_per_24h": cfg.litter_visits_alert_per_24h,
        "cameras": cams,
    }


@app.post("/api/system/health-detectors")
async def api_system_health_detectors_update(request: Request, payload: dict):
    """Persist health-detector config. Empty `litter_box_camera` turns
    the feature off; an unknown camera name is a 400 (the UI should
    only POST values from the dropdown)."""
    _require_auth(request)
    cfg = config_store.load_config()
    cams = {c.name for c in camera_store.load()}
    if "litter_box_camera" in payload:
        raw = (payload.get("litter_box_camera") or "").strip()
        if raw and raw not in cams:
            raise HTTPException(status_code=400, detail=f"unknown camera {raw!r}")
        cfg.litter_box_camera = raw
    if "litter_visits_alert_per_24h" in payload:
        try:
            n = int(payload.get("litter_visits_alert_per_24h"))
            if not 1 <= n <= 200:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail="litter_visits_alert_per_24h must be an integer 1..200")
        cfg.litter_visits_alert_per_24h = str(n)
    config_store.save_config(cfg)
    return {"ok": True}


@app.post("/api/frigate/restart")
async def api_frigate_restart(request: Request):
    _require_auth(request)
    cfg = config_store.load_config()
    cameras = camera_store.load()
    if not config_store.is_setup_complete(cfg, cameras):
        raise HTTPException(status_code=400, detail="setup not complete (need at least one camera)")
    config_store.write_frigate_config(cfg, cameras)
    try:
        docker_ops.restart_frigate()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/frigate/logs", response_class=Response)
async def api_frigate_logs(request: Request):
    _require_auth(request)
    text = docker_ops.recent_frigate_logs(tail=300)
    return Response(content=text, media_type="text/plain; charset=utf-8")


@app.get("/api/admin/logs", response_class=Response)
async def api_admin_logs(request: Request):
    """Admin-container logs — for debugging when something is silently
    failing (e.g. health monitor, recognition errors). Read-only,
    last 300 lines."""
    _require_auth(request)
    try:
        client = docker_ops._client()  # noqa: SLF001
        c = client.containers.get("pawcorder-admin")
        text = c.logs(tail=300, stream=False, follow=False).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        text = f"<could not read admin logs: {exc}>"
    return Response(content=text, media_type="text/plain; charset=utf-8")


@app.get("/api/pets/correlation")
async def api_pets_correlation(request: Request, hours: float = 24.0):
    """Pairwise overlap of pets seen on the same camera at the same
    time. UI uses this for 'Mochi & Maru spent 2h together today'."""
    _require_auth(request)
    pairs = insights.cross_pet_correlation(since_hours=float(hours))
    return {"pairs": [p.to_dict() for p in pairs]}


@app.get("/api/system/bandwidth")
async def api_system_bandwidth(request: Request):
    """Per-camera bandwidth estimate from Frigate's /api/stats."""
    _require_auth(request)
    rows = await insights.bandwidth_per_camera()
    return {"cameras": [r.to_dict() for r in rows]}


@app.get("/api/energy-mode")
async def api_energy_get(request: Request):
    _require_auth(request)
    return insights.load_energy_mode().to_dict()


@app.post("/api/energy-mode")
async def api_energy_save(request: Request, payload: dict):
    _require_auth(request)
    mode = insights.EnergyMode(
        enabled=bool(payload.get("enabled")),
        schedules=[
            insights.EnergySchedule(
                cameras=[str(c) for c in (s.get("cameras") or []) if isinstance(c, str)],
                start_hour=int(s.get("start_hour") or 0),
                end_hour=int(s.get("end_hour") or 0),
            )
            for s in (payload.get("schedules") or []) if isinstance(s, dict)
        ],
    )
    insights.save_energy_mode(mode)
    return mode.to_dict()


@app.get("/api/pets/today")
async def api_pets_today(request: Request, limit: int = 5):
    """Top-N highest-confidence sightings from the last 24 h, with a
    snapshot URL for each. UI uses this for the 'today's moments'
    section on /pets."""
    _require_auth(request)
    rows = recognition.read_sightings(limit=10_000, since=time.time() - 86400)
    # Sort by score (top first), keep the highest per event_id.
    seen_event_ids: set = set()
    out: list[dict] = []
    rows.sort(key=lambda r: r.get("score", 0), reverse=True)
    for r in rows:
        eid = r.get("event_id")
        if not eid or eid in seen_event_ids:
            continue
        seen_event_ids.add(eid)
        out.append({
            "event_id": eid,
            "camera": r.get("camera"),
            "pet_name": r.get("pet_name"),
            "pet_id": r.get("pet_id"),
            "score": r.get("score"),
            "confidence": r.get("confidence"),
            "start_time": r.get("start_time"),
            "snapshot_url": f"/api/frigate/snapshot/{eid}",
        })
        if len(out) >= max(1, min(limit, 20)):
            break
    return {"moments": out}


@app.get("/api/frigate/snapshot/{event_id}", response_class=Response)
async def api_frigate_snapshot(request: Request, event_id: str):
    """Proxy a Frigate event snapshot — same pattern as the camera
    thumbnail proxy, lets the user's browser stay on admin's origin."""
    _require_auth(request)
    # Frigate event_ids look like "1730000000.123456-camera_name-cat".
    # They contain dots and hyphens but never slashes or whitespace.
    # Whitelist [a-zA-Z0-9._-]+ — anything else is a path-traversal
    # attempt or garbled query.
    import re
    if not event_id or not re.fullmatch(r"[A-Za-z0-9._-]+", event_id) or ".." in event_id:
        raise HTTPException(status_code=400, detail="bad event id")
    url = f"{tg.FRIGATE_BASE_URL}/api/events/{event_id}/snapshot.jpg"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url, params={"bbox": 1})
        if resp.status_code != 200 or not resp.content:
            raise HTTPException(status_code=404, detail="snapshot unavailable")
        return Response(content=resp.content, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="snapshot proxy failed")


@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page(request: Request):
    """Shown after the first-time setup wizard finishes. Three big
    next-step cards. Self-dismissing — just navigate away."""
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("welcome.html", request)


@app.get("/pets/health", response_class=HTMLResponse)
async def pets_health_page(request: Request):
    """Per-pet health overview with charts. Aggregates every health
    signal (presence, activity, litter, bowls, posture, fights) into
    one page so an owner doesn't have to chase notifications."""
    if not auth.is_authenticated(request):
        return _redirect_login()
    from . import pet_health_overview, recognition, reenroll
    overviews = pet_health_overview.overview_for_all_pets(
        lang=i18n.get_lang_from_request(request),
    )
    recognition_stale_count = reenroll.stale_count()
    # Cloud-trained V2 models are tied to a specific backbone. After a
    # backbone swap the old models silently stop helping — surface the
    # count so the operator knows which pets need re-training.
    stale_cloud_pets = recognition.stale_cloud_models()
    # Hint banner is shown only when nothing related to bowls / litter
    # rendered for any pet — once the owner draws a zone, the matching
    # sparkline appears and the banner self-hides.
    any_bowl_or_litter = any(
        ov.sparkline_litter_svg or ov.sparkline_water_svg or ov.sparkline_food_svg
        for ov in overviews
    )
    return _render(
        "pets_health.html", request,
        overviews=overviews,
        any_bowl_or_litter=any_bowl_or_litter,
        recognition_stale_count=recognition_stale_count,
        stale_cloud_pets=stale_cloud_pets,
        uptime_svg=pet_health_overview.system_uptime_ribbon(days=7),
    )


@app.get("/recognition", response_class=HTMLResponse)
async def recognition_page(request: Request):
    """Read-only diagnostics for the recognition pipeline.

    Shows score histograms per pet, multi-frame coverage, cloud-boost
    activity, confidence mix — so an owner can see what the AI
    upgrades are actually doing on their footage rather than trusting
    the marketing claims.
    """
    if not auth.is_authenticated(request):
        return _redirect_login()
    from . import recognition_stats
    diag = recognition_stats.build()
    return _render(
        "recognition.html", request,
        diag=diag.to_dict(),
    )


@app.get("/api/recognition/stats")
async def api_recognition_stats(request: Request):
    """JSON shape of /recognition. Useful for ad-hoc queries / external
    dashboards."""
    _require_auth(request)
    from . import recognition_stats
    return recognition_stats.build().to_dict()


@app.get("/api/pets/health")
async def api_pets_health(request: Request):
    """Per-pet health snapshots: presence (last seen / activity), litter
    box visits, recent suspicious co-sighting clusters. Each section is
    individually optional — OSS builds with no Pro modules drop in
    return all-empty arrays so the /pets page renders without a
    cascade of feature flags in the template."""
    _require_auth(request)
    # Read the sightings log once and feed every detector. The widest
    # window any detector needs is pet_health's BASELINE_DAYS+1 (8d);
    # narrower detectors filter from that slice in-memory.
    now = time.time()
    if pet_health is not None:
        widest_seconds = (pet_health.BASELINE_DAYS + 1) * 86400
    else:
        widest_seconds = 86400
    rows_widest = recognition.read_sightings(
        limit=20_000, since=now - widest_seconds,
    )
    rows_24h = [r for r in rows_widest
                if float(r.get("start_time") or 0) >= now - 86400]
    fight_lookback = (fight_detector.LOOKBACK_SECONDS
                      if fight_detector is not None else 600)
    rows_recent = [r for r in rows_24h
                   if float(r.get("start_time") or 0) >= now - fight_lookback]
    rows_1h = [r for r in rows_24h
                if float(r.get("start_time") or 0) >= now - 3600]
    posture: list[dict] = []
    if posture_detector is not None:
        posture = [s.to_dict()
                   for s in posture_detector.vomit_snapshots(now=now, rows=rows_1h)]
        posture += [s.to_dict()
                    for s in posture_detector.gait_snapshots(now=now, rows=rows_1h)]
    bowls: list[dict] = []
    if bowl_monitor is not None:
        # Bowl baselines reach back BASELINE_DAYS+1 just like pet_health,
        # so the widest slice already covers it.
        bowls = [s.to_dict() for s in bowl_monitor.snapshots_all(now=now, rows=rows_widest)]
    return {
        "snapshots": [s.to_dict() for s in pet_health.snapshots_all(now=now, rows=rows_widest)] if pet_health else [],
        "litter": [s.to_dict() for s in litter_monitor.snapshots_all(now=now, rows=rows_24h)] if litter_monitor else [],
        "fight_clusters": [c.to_dict() for c in fight_detector._scan_clusters(rows_recent)] if fight_detector else [],
        "posture": posture,
        "bowls": bowls,
    }


@app.get("/api/pets/diary")
async def api_pets_diary_list(request: Request, pet_id: str | None = None,
                               limit: int = 30):
    """List recent diary entries, optionally filtered to one pet. Returns
    `{"configured": false}` if neither OpenAI nor Pro license is set —
    the UI uses this to surface a setup nudge."""
    _require_auth(request)
    cfg = config_store.load_config()
    configured = bool(cfg.ollama_base_url or cfg.openai_api_key or cfg.pawcorder_pro_license_key)
    return {
        "configured": configured,
        "backend": pet_diary.active_backend(cfg),
        "diaries": pet_diary.read_diaries(pet_id=pet_id, limit=max(1, min(limit, 200))),
    }


@app.post("/api/pets/diary/generate")
async def api_pets_diary_generate(request: Request, pet_id: str = Form(...)):
    """Generate a diary on-demand (the daily scheduler runs at 22:00,
    but the user can hit "refresh" any time). Body: pet_id."""
    _require_auth(request)
    pet = pets_store.PetStore().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="pet_not_found")
    lang = i18n.get_lang_from_request(request)
    try:
        d = await pet_diary.generate_diary(pet, lang=lang)
    except pet_diary.DiaryNotConfigured:
        raise HTTPException(status_code=400, detail="diary_not_configured")
    except RuntimeError as exc:
        # Don't echo the upstream provider's response body back to the
        # client — it can include API-key fragments and stack info.
        # Log it server-side; user just gets a generic backend error.
        import logging
        logging.getLogger("pawcorder.main").warning(
            "pet diary backend failed for %s: %s", pet.pet_id, exc,
        )
        raise HTTPException(status_code=502, detail="diary_backend_error")
    pet_diary.append_diary(d)
    return d.to_dict()


@app.get("/reliability", response_class=HTMLResponse)
async def page_reliability(request: Request):
    """Operator-facing SLO dashboard. Shows per-camera uptime, AI
    inference success rate, and push-delivery rate for the last 7 days.

    Renders inside the admin shell so the user can navigate back. The
    actual data is computed in :mod:`reliability` and shipped as JSON
    to the page so the table can be sorted client-side without a refetch.
    """
    if not auth.is_authenticated(request):
        return _redirect_login()
    summary = reliability.summarize()
    return _render("reliability.html", request, summary=summary)


@app.get("/api/reliability")
async def api_reliability(request: Request, days: int = 7):
    """JSON of the SLO summary. Used by the page for live refresh, and
    available to external monitoring (Home Assistant gauge, etc.) via
    the same Bearer auth as everything else."""
    _require_auth(request)
    days = max(1, min(int(days or 7), 90))
    return reliability.summarize(days=days)


@app.post("/api/pets/calibrate")
async def api_pets_calibrate(request: Request):
    """Run local recognition calibration: sweeps each pet's photos
    against itself + every other pet to pick a per-pet cosine
    threshold that minimises confusion. Stores chosen_threshold
    + diagnostics on each Pet record. Available to OSS + Pro;
    pure local math, no relay round-trip."""
    _require_auth(request)
    try:
        from .pro import finetune
    except ImportError:
        raise HTTPException(status_code=503, detail="finetune_unavailable")
    store = pets_store.PetStore()
    pets = store.load()
    if not pets:
        raise HTTPException(status_code=400, detail="no_pets")
    results = finetune.calibrate_all(pets=pets)
    by_id = {r.pet_id: r for r in results}
    for p in pets:
        r = by_id.get(p.pet_id)
        if r is None:
            continue
        # Only adopt the calibration when there's enough data to be
        # meaningful — sample_pairs_intra=0 means a one-photo pet,
        # for whom we deliberately keep the global threshold.
        if r.sample_pairs_intra > 0:
            p.match_threshold = r.chosen_threshold
        p.calibration = r.to_dict()
    store.save_all(pets)
    return {"results": [r.to_dict() for r in results]}


@app.get("/api/pets/podcasts")
async def api_pets_podcasts(request: Request):
    """List recent weekly podcast episodes (newest first)."""
    _require_auth(request)
    return {"podcasts": podcast.list_podcasts()}


@app.post("/api/pets/podcasts/generate")
async def api_pets_podcasts_generate(request: Request):
    """Manually trigger a podcast for today. Useful for both end users
    ("I want one now") and ops smoke-testing the relay TTS pipeline."""
    _require_auth(request)
    cfg = config_store.load_config()
    if not cfg.pawcorder_pro_license_key:
        raise HTTPException(status_code=400, detail="pro_license_required")
    pets = pets_store.PetStore().load()
    if not pets:
        raise HTTPException(status_code=400, detail="no_pets_configured")
    diaries = pet_diary.read_diaries(limit=100)
    import time as _t
    cutoff = _t.strftime("%Y-%m-%d", _t.localtime(_t.time() - 7 * 86400))
    diaries = [d for d in diaries if (d.get("date") or "") >= cutoff]
    lang = i18n.get_lang_from_request(request)
    script, covered = podcast.build_script(pets=pets, diaries=diaries, lang=lang)
    try:
        audio = await podcast.synthesize(script, cfg.pawcorder_pro_license_key)
    except RuntimeError as exc:
        import logging
        logging.getLogger("pawcorder.main").warning("podcast TTS failed: %s", exc)
        raise HTTPException(status_code=502, detail="tts_failed")
    today = _t.strftime("%Y-%m-%d", _t.localtime())
    p = podcast.Podcast(
        date=today, script=script, audio_path="",
        pets_covered=covered, generated_at=_t.time(),
    )
    podcast.save_podcast(p, audio)
    return p.to_dict()


@app.get("/api/pets/podcasts/{date}/audio")
async def api_pets_podcasts_audio(request: Request, date: str):
    """Stream the mp3 for one episode. Date must match `^\\d{4}-\\d{2}-\\d{2}$`
    so we can't be tricked into reading a parent dir."""
    _require_auth(request)
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=400, detail="bad_date")
    p = podcast.PODCAST_DIR / f"{date}.mp3"
    if not p.exists():
        raise HTTPException(status_code=404, detail="not_found")
    return Response(content=p.read_bytes(), media_type="audio/mpeg",
                     headers={"Content-Disposition": f'inline; filename="pawcorder-{date}.mp3"'})


@app.post("/api/pets/query")
async def api_pets_query(request: Request, payload: dict):
    """Natural-language Q&A over the sightings timeline.

    Body: {"question": "Did Mochi jump on the table today?"}
    Response: {"answer": "...", "event_ids": [...], "backend": "...",
               "samples_considered": N, "window_hours": ...}
    """
    _require_auth(request)
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question_required")
    lang = i18n.get_lang_from_request(request)
    try:
        answer = await pet_query.answer_question(question, lang=lang)
    except pet_diary.DiaryNotConfigured:
        raise HTTPException(status_code=400, detail="diary_not_configured")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        import logging
        logging.getLogger("pawcorder.main").warning(
            "query backend failed: %s", exc,
        )
        raise HTTPException(status_code=502, detail="query_backend_error")
    return answer.to_dict()


@app.get("/pets/{pet_id}/train-cloud", response_class=HTMLResponse)
async def page_pet_train_cloud(request: Request, pet_id: str):
    """Pro: per-pet cloud training page. Owner uploads reference photos
    + ticks consent; we ship them to the relay, get a tiny classifier
    back. The page also shows status of any in-flight training job."""
    if not auth.is_authenticated(request):
        return _redirect_login()
    pet = pets_store.PetStore().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="pet_not_found")
    from . import cloud_train, recognition
    cfg = config_store.load_config()
    has_pro = bool(cfg.pawcorder_pro_license_key)
    lang = i18n.get_lang_from_request(request)
    consent_text = i18n.t("CLOUD_TRAIN_CONSENT_BODY", lang=lang)
    # Surface whether the pet has a real V2 classifier vs a V1 placeholder
    # vs nothing yet — the template renders different copy per case so
    # the owner sees an accurate "Custom model: trained" badge.
    model_status = recognition.cloud_model_status(pet_id)
    return _render(
        "pets_train.html", request,
        pet=pet.to_dict(),
        consent_text=consent_text,
        initial_state=cloud_train.latest_state(pet_id).to_dict(),
        has_pro=has_pro,
        model_status=model_status,
    )


@app.post("/api/pets/{pet_id}/train-cloud/upload")
async def api_pet_train_upload(request: Request, pet_id: str):
    """Forward owner-uploaded photos to the relay. Validates types +
    sizes server-side so a forged client can't push junk through."""
    _require_auth(request)
    pet = pets_store.PetStore().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="pet_not_found")
    from . import cloud_train
    form = await request.form()
    consent_hash = (form.get("consent_hash") or "").strip()
    # Recompute the consent hash for every supported language and
    # require the client's hash to match one of them. We iterate
    # ``i18n.SUPPORTED`` (not a hard-coded en/zh-TW pair) so that
    # adding a Japanese or Korean translation later doesn't silently
    # reject those owners' clicks. ``i18n.t`` falls back to en for any
    # lang that doesn't have its own translation; the resulting set
    # dedupes naturally.
    expected_hashes = {
        cloud_train.consent_hash(i18n.t("CLOUD_TRAIN_CONSENT_BODY", lang=lang))
        for lang in i18n.SUPPORTED
    }
    if consent_hash not in expected_hashes:
        raise HTTPException(status_code=400, detail="consent_required")
    files = form.getlist("photos") if hasattr(form, "getlist") else []
    if not files:
        raise HTTPException(status_code=400, detail="cloud_train_no_photos")
    # Cap the count BEFORE reading any bodies — otherwise a thousand
    # 1-byte uploads could DoS memory before the per-file check fires.
    if len(files) > cloud_train.MAX_TOTAL_PHOTOS:
        raise HTTPException(status_code=400,
                              detail="cloud_train_too_many_photos")
    photos: list[tuple[str, bytes, str]] = []
    for f in files:
        if not hasattr(f, "read"):
            continue
        # Read the body and check its size INSIDE the loop. Each file
        # is bounded by MAX_PHOTO_BYTES; total memory use is bounded
        # by MAX_PHOTO_BYTES × MAX_TOTAL_PHOTOS = 640 MB worst case.
        body = await f.read()
        if len(body) > cloud_train.MAX_PHOTO_BYTES:
            raise HTTPException(status_code=400,
                                  detail="cloud_train_file_too_big")
        mime = getattr(f, "content_type", "") or "application/octet-stream"
        filename = getattr(f, "filename", "") or "photo"
        err = cloud_train.validate_file(filename, body, mime)
        if err:
            raise HTTPException(status_code=400, detail=err)
        photos.append((filename, body, mime))
    try:
        state = await cloud_train.upload_photos(
            pet_id, photos, consent_text_hash=consent_hash,
        )
    except cloud_train.CloudTrainError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return state.to_dict()


@app.get("/api/pets/{pet_id}/train-cloud/status")
async def api_pet_train_status(request: Request, pet_id: str):
    """Poll for the latest job status. Falls back to the local ledger
    if the relay is unreachable so the page still renders."""
    _require_auth(request)
    pet = pets_store.PetStore().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="pet_not_found")
    from . import cloud_train
    cfg = config_store.load_config()
    if not cfg.pawcorder_pro_license_key:
        return cloud_train.latest_state(pet_id).to_dict()
    try:
        state = await cloud_train.poll_status(pet_id)
    except cloud_train.CloudTrainError:
        state = cloud_train.latest_state(pet_id)
    return state.to_dict()


@app.post("/api/pets/{pet_id}/train-cloud/forget")
async def api_pet_train_forget(request: Request, pet_id: str):
    """Owner-triggered purge: relay deletes photos, we delete the local
    model. State resets to idle."""
    _require_auth(request)
    # Pet existence check matches the other train-cloud routes; without
    # it, a malformed pet_id could land in cloud_train's local-path
    # construction. (See cloud_train._local_model_path.)
    pet = pets_store.PetStore().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="pet_not_found")
    from . import cloud_train
    try:
        state = await cloud_train.request_delete(pet_id)
    except cloud_train.CloudTrainError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return state.to_dict()


@app.get("/pets/{pet_id}/vet-pack", response_class=HTMLResponse)
async def page_vet_pack(request: Request, pet_id: str):
    """Standalone printable 30-day health summary for the vet visit.

    Renders OUTSIDE the admin shell (no nav, no chrome) because the
    user is going to print it / save as PDF — admin links and dark-mode
    bars don't belong on the printout. The HTML is its own document
    with inline CSS, no external assets.
    """
    _require_auth(request)
    pet = pets_store.PetStore().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="pet_not_found")
    lang = i18n.get_lang_from_request(request)
    pack = vet_pack.build_vet_pack(pet)
    return HTMLResponse(content=vet_pack.render_html(pack, lang=lang))


@app.post("/api/pets/{pet_id}/vet-pack/share")
async def api_vet_pack_share(request: Request, pet_id: str):
    """Mint a 24h signed link for the vet pack. Body: empty.

    Returns ``{"url": "/share/vet-pack/<pet>?t=<token>", "expires_at": ts}``.
    The owner copies this and texts it to the vet — vet opens it on
    their own device, no Pawcorder login needed."""
    _require_auth(request)
    pet = pets_store.PetStore().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="pet_not_found")
    try:
        token = vet_pack.mint_share_token(pet_id)
    except RuntimeError as exc:
        # Caller's setup is incomplete (no admin session secret yet).
        raise HTTPException(status_code=503, detail=str(exc))
    expires_at = int(token.split(".", 1)[0])
    base = str(request.base_url).rstrip("/")
    return {
        "url": f"{base}/share/vet-pack/{pet_id}?t={token}",
        "expires_at": expires_at,
    }


@app.get("/share/vet-pack/{pet_id}", response_class=HTMLResponse)
async def page_vet_pack_shared(request: Request, pet_id: str, t: str = ""):
    """Public-but-token-gated rendering of the vet pack. Same HTML as
    the auth'd route, but bypasses session auth on a valid signature.
    Wrong / expired token returns 410 Gone (link expired) so a vet
    knows to ask the owner for a fresh share."""
    if not vet_pack.verify_share_token(pet_id, t):
        raise HTTPException(status_code=410, detail="share_link_expired")
    pet = pets_store.PetStore().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="pet_not_found")
    lang = i18n.get_lang_from_request(request)
    pack = vet_pack.build_vet_pack(pet)
    return HTMLResponse(content=vet_pack.render_html(pack, lang=lang))


@app.get("/timelapse", response_class=HTMLResponse)
async def timelapse_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("timelapse.html", request, items=timelapse.list_timelapses())


@app.get("/api/timelapse")
async def api_timelapse_list(request: Request):
    _require_auth(request)
    return {"items": timelapse.list_timelapses()}


@app.get("/api/timelapse/{filename}", response_class=Response)
async def api_timelapse_download(request: Request, filename: str):
    """Serve a built time-lapse mp4 — same shape as highlights download."""
    _require_auth(request)
    import re
    if not re.match(r"^[A-Za-z0-9_\-]+-\d{4}-\d{2}-\d{2}\.mp4$", filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = timelapse.storage_root() / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="timelapse not found")
    return Response(content=path.read_bytes(), media_type="video/mp4",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/timelapse/build-now")
async def api_timelapse_build_now(request: Request):
    """Force-build yesterday's timelapses now (useful for demo)."""
    _require_auth(request)
    if not timelapse.ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg not on PATH")
    results = timelapse.build_yesterday()
    return {"results": [r.to_dict() for r in results]}


@app.get("/api/highlights")
async def api_highlights_list(request: Request):
    """Recent highlight reels — newest first. Used by /pets page."""
    _require_auth(request)
    return {"highlights": highlights.list_highlights()}


@app.get("/api/highlights/{filename}", response_class=Response)
async def api_highlights_download(request: Request, filename: str):
    """Stream a highlight mp4 for inline playback / download."""
    _require_auth(request)
    # filename comes through FastAPI as a single path segment, no
    # slashes possible. Belt-and-braces: reject anything except a
    # date-named mp4.
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2}\.mp4$", filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = highlights.output_dir() / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="highlight not found")
    return Response(
        content=path.read_bytes(),
        media_type="video/mp4",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/api/highlights/build-now")
async def api_highlights_build_now(request: Request):
    """Manually trigger today's reel — useful for "let me see the demo right now"."""
    _require_auth(request)
    if not highlights.ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg not on PATH")
    now = time.time()
    result = await highlights.build_highlights_for(now - 86400, now)
    return result.to_dict()


@app.put("/api/cameras/{name}/ptz/presets")
async def api_camera_ptz_presets_save(request: Request, name: str, payload: dict):
    """Replace the saved-preset list. Body: {"presets": [{"name":"feeding"}, ...]}.
    Frigate uses ONVIF to actually park the lens — we just persist the
    user-friendly name + the ONVIF preset_token. The token is whatever
    Frigate's PUT /api/<cam>/ptz?action=preset_save returns; we accept
    it client-side via the response of that endpoint.
    """
    _require_auth(request)
    cam = camera_store.get(name)
    if not cam:
        raise HTTPException(status_code=404, detail="camera not found")
    presets = payload.get("presets")
    if not isinstance(presets, list):
        raise HTTPException(status_code=400, detail="presets must be a list")
    cam.ptz_presets = list(presets)
    try:
        camera_store.update(name, cam)
    except CameraValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "presets": cam.ptz_presets}


@app.post("/api/cameras/{name}/ptz/preset-save")
async def api_camera_ptz_preset_save(request: Request, name: str, payload: dict):
    """Save the camera's CURRENT pan/tilt/zoom position as a preset.
    Asks Frigate to park-and-record via ONVIF, then appends the
    preset to camera.ptz_presets so the UI can show a quick-jump."""
    _require_auth(request)
    cam = camera_store.get(name)
    if not cam:
        raise HTTPException(status_code=404, detail="camera not found")
    label = (payload.get("name") or "").strip()
    if not label or len(label) > 32 or not label.replace("_", "").replace(" ", "").isalnum():
        raise HTTPException(status_code=400, detail="preset name must be 1-32 alphanumeric chars")
    # Frigate's preset_save action takes the desired token in the
    # `preset` query param. We pick a stable slug from the label.
    preset_token = label.lower().replace(" ", "_")
    url = f"{tg.FRIGATE_BASE_URL}/api/{name}/ptz"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.put(url, params={"action": "preset_save", "preset": preset_token})
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Frigate refused preset save: HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"PTZ unreachable: {exc}")
    # Add to the camera's persisted presets, dedupe by token.
    cam.ptz_presets = [p for p in (cam.ptz_presets or []) if p.get("preset_token") != preset_token]
    cam.ptz_presets.append({"name": label, "preset_token": preset_token})
    camera_store.update(name, cam)
    return {"ok": True, "preset": {"name": label, "preset_token": preset_token}}


@app.post("/api/cameras/{name}/ptz")
async def api_camera_ptz(request: Request, name: str, payload: dict):
    """Pan / tilt / zoom command — proxied to Frigate's PTZ API.

    Body: {"action": "move", "dir": "left" | "right" | "up" | "down" | "stop"}
       or {"action": "preset", "preset": "feeding_spot"}
       or {"action": "zoom", "dir": "in" | "out" | "stop"}

    Frigate exposes ONVIF PTZ at /api/<cam>/ptz?action=... — we just
    relay so the admin UI doesn't need to expose Frigate's port.
    """
    _require_auth(request)
    cam = camera_store.get(name)
    if not cam:
        raise HTTPException(status_code=404, detail="camera not found")
    action = (payload.get("action") or "").strip().lower()
    if action not in ("move", "stop", "preset", "zoom"):
        raise HTTPException(status_code=400, detail="action must be move/stop/preset/zoom")

    # Map our compact body to Frigate's URL params.
    params: dict[str, str] = {}
    if action == "move":
        d = (payload.get("dir") or "").strip().lower()
        if d not in ("left", "right", "up", "down", "stop"):
            raise HTTPException(status_code=400, detail="bad dir")
        params["action"] = d if d == "stop" else f"MOVE_{d.upper()}"
    elif action == "stop":
        params["action"] = "stop"
    elif action == "preset":
        preset = (payload.get("preset") or "").strip()
        if not preset:
            raise HTTPException(status_code=400, detail="preset name required")
        params["action"] = "preset"
        params["preset"] = preset
    elif action == "zoom":
        d = (payload.get("dir") or "").strip().lower()
        if d not in ("in", "out", "stop"):
            raise HTTPException(status_code=400, detail="bad zoom dir")
        params["action"] = "zoom_in" if d == "in" else ("zoom_out" if d == "out" else "stop")

    url = f"{tg.FRIGATE_BASE_URL}/api/{name}/ptz"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.put(url, params=params)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Frigate PTZ returned {resp.status_code}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"PTZ unreachable: {exc}")
    return {"ok": True}


@app.get("/api/cameras/{name}/stream")
async def api_camera_stream(request: Request, name: str):
    """Proxy Frigate's MJPEG stream. We stream it through the admin
    so the user's browser stays on the admin's origin (no Frigate
    port needed for remote access via Tailscale).

    MJPEG is the lowest-common-denominator live-view: every browser
    plays it via plain `<img src=...>`, no JS, no codec, no WebRTC
    handshake. Latency 1-3 s, totally fine for "is the cat OK".
    """
    _require_auth(request)
    if not camera_store.get(name):
        raise HTTPException(status_code=404, detail="camera not found")
    url = f"{tg.FRIGATE_BASE_URL}/api/{name}"  # /api/<cam> = MJPEG
    # We can't return the streaming response directly because httpx's
    # AsyncClient context manager closes when this function returns.
    # Use StreamingResponse with the underlying iterator.
    from fastapi.responses import StreamingResponse

    async def _proxy():
        # Soft-fail on every kind of network mishap (Frigate down, DNS
        # not resolving in demo mode, transient TCP reset). The browser
        # will see an empty stream and silently retry — much nicer than
        # a 500 + Python traceback in the server log.
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return
                    async for chunk in resp.aiter_raw():
                        yield chunk
        except (httpx.HTTPError, OSError) as exc:
            import logging
            logging.getLogger("pawcorder").info(
                "live stream unavailable for %s: %s", name, exc,
            )
            return

    return StreamingResponse(
        _proxy(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/cameras/{name}/zones", response_class=HTMLResponse)
async def camera_zones_page(request: Request, name: str):
    if not auth.is_authenticated(request):
        return _redirect_login()
    # Editing zones is admin-only — match the PUT route's role gate so
    # family/kid users don't see a UI they can't actually save from.
    role = users.role_from_request(request) or "admin"
    if not users.has_role(role, "admin"):
        return _render_html_error(request, 403, "admins only")
    cam = camera_store.get(name)
    if not cam:
        return _render_html_error(request, 404, f"camera {name} not found")
    return _render(
        "camera_zones.html", request,
        camera=cam.to_dict(),
    )


@app.put("/api/cameras/{name}/zones")
async def api_camera_zones_save(request: Request, name: str, payload: dict):
    """Replace the camera's zones + privacy_masks atomically.
    Body: {"zones": [...], "privacy_masks": [...]}
    """
    _require_role(request, min_role="admin")
    cam = camera_store.get(name)
    if not cam:
        raise HTTPException(status_code=404, detail="camera not found")
    zones = payload.get("zones")
    masks = payload.get("privacy_masks")
    if zones is not None:
        if not isinstance(zones, list):
            raise HTTPException(status_code=400, detail="zones must be a list")
        # Normalize ``kind`` server-side so a malformed client (or a
        # legacy zone passed back through edit) can't poison the YAML
        # with an unknown purpose. Unknown / missing falls to "detect".
        clean_zones: list[dict] = []
        for z in zones:
            if not isinstance(z, dict):
                continue
            kind = z.get("kind") or cameras_store.ZONE_KIND_DETECT
            if kind not in cameras_store.ZONE_KINDS:
                kind = cameras_store.ZONE_KIND_DETECT
            z["kind"] = kind
            clean_zones.append(z)
        cam.zones = clean_zones
    if masks is not None:
        if not isinstance(masks, list):
            raise HTTPException(status_code=400, detail="privacy_masks must be a list")
        cam.privacy_masks = list(masks)
    try:
        camera_store.update(name, cam)
    except CameraValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _rerender_and_restart()
    return {"ok": True, "zones": cam.zones, "privacy_masks": cam.privacy_masks}


@app.get("/api/cameras/{name}/heatmap", response_class=Response)
async def api_camera_heatmap(request: Request, name: str, force: bool = False):
    """Activity heatmap as a translucent PNG. UI composites it over
    the latest.jpg thumbnail. Server-side cached for 1 h per camera."""
    _require_auth(request)
    if not camera_store.get(name):
        raise HTTPException(status_code=404, detail="camera not found")
    png, meta = await heatmap.get_or_build_png(name, force=bool(force))
    return Response(
        content=png, media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Heatmap-Samples": str(meta.get("sample_count", 0)),
            "X-Heatmap-Generated": str(int(meta.get("generated_at", 0))),
        },
    )


@app.get("/api/cameras/{name}/thumbnail", response_class=Response)
async def api_camera_thumbnail(request: Request, name: str):
    """Proxy Frigate's latest.jpg for a camera, with a short cache.

    Browsers can't reach Frigate (port 5000) directly without
    Tailscale / port forward, but they CAN reach the admin (port
    8080). Proxy keeps the same-origin flow working."""
    _require_auth(request)
    if not camera_store.get(name):
        raise HTTPException(status_code=404, detail="camera not found")
    url = f"{tg.FRIGATE_BASE_URL}/api/{name}/latest.jpg"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.content:
            raise HTTPException(status_code=502, detail="thumbnail unavailable")
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "private, max-age=10"},
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="thumbnail unavailable")


@app.get("/api/platform")
async def api_platform(request: Request):
    """Re-run platform detection (for the /hardware page refresh button)."""
    _require_auth(request)
    info = platform_detect.detect()
    return {
        "platform": info.to_dict(),
        "recommended_detector": platform_detect.recommended_detector(info),
        "valid_detectors": list(platform_detect.VALID_DETECTORS),
    }


@app.get("/api/qrcode", response_class=Response)
async def api_qrcode(request: Request, url: str):
    _require_auth(request)
    if len(url) > 512 or not url:
        raise HTTPException(status_code=400, detail="invalid url")
    qr = qrcode.QRCode(box_size=10, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    buffer = io.BytesIO()
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    img.save(buffer)
    return Response(content=buffer.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/notifications/test")
async def api_notifications_test(request: Request, payload: dict):
    _require_auth(request)
    cfg = config_store.load_config()
    channel = (payload.get("channel") or "telegram").strip()
    if channel == "telegram":
        token = (payload.get("telegram_bot_token") or cfg.telegram_bot_token).strip()
        chat_id = (payload.get("telegram_chat_id") or cfg.telegram_chat_id).strip()
        if not token or not chat_id:
            raise HTTPException(status_code=400, detail="bot token and chat id are required")
        result = await tg.send_test(token, chat_id)
    elif channel == "line":
        token = (payload.get("line_channel_token") or cfg.line_channel_token).strip()
        target = (payload.get("line_target_id") or cfg.line_target_id).strip()
        if not token or not target:
            raise HTTPException(status_code=400, detail="channel token and target id are required")
        result = await line_api.send_test(token, target)
    else:
        raise HTTPException(status_code=400, detail=f"unknown channel {channel!r}")
    if not result.ok:
        return JSONResponse(status_code=400, content={"ok": False, "error": result.error})
    return {"ok": True}


# ---- marketing signup (public endpoint) --------------------------------

# Permissive CORS for the public signup endpoint only — marketing site
# may be hosted at a different origin and needs the browser to honour
# the response. Every other route stays same-origin only.
_MARKETING_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Vary": "Origin",
}


@app.post("/api/marketing/signup")
async def api_marketing_signup(request: Request, payload: dict | None = None):
    """Public — no auth, no CSRF. The pre-launch landing page POSTs here.

    CORS is open because the marketing static site can be hosted on a
    separate subdomain. Per-IP rate limit (5/h) lives in marketing.py to
    keep simple bots out without DOS-protecting the whole route.
    """
    payload = payload or {}
    ip = request.client.host if request.client else ""
    result = marketing.record_signup(
        email=str(payload.get("email") or ""),
        source=str(payload.get("source") or "landing"),
        locale=str(payload.get("locale") or ""),
        ip=ip,
    )
    if not result.ok:
        status = 429 if result.rate_limited else 400
        return JSONResponse(
            status_code=status,
            content={"ok": False, "error": result.error},
            headers=_MARKETING_CORS_HEADERS,
        )
    return JSONResponse(
        content={"ok": True, "duplicate": result.duplicate},
        headers=_MARKETING_CORS_HEADERS,
    )


@app.options("/api/marketing/signup")
async def api_marketing_signup_options():
    """Preflight for cross-origin POST from the marketing site."""
    return Response(content="", media_type="text/plain", headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    })


@app.get("/api/marketing/signups")
async def api_marketing_signups(request: Request):
    """Auth-required — admin can pull the list of signups for export."""
    _require_auth(request)
    return {"signups": marketing.list_signups()}


# ---- pets / recognition -------------------------------------------------

pet_store = pets_store.PetStore()


@app.get("/pets", response_class=HTMLResponse)
async def pets_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    pets = pet_store.load()
    summary = timeline.cross_camera_summary(since_hours=24)
    model_present = embeddings.model_path().exists()
    return _render(
        "pets.html", request,
        pets=[p.to_dict() for p in pets],
        summary=summary,
        recognition_ready=model_present,
    )


@app.get("/api/pets")
async def api_pets_list(request: Request):
    _require_auth(request)
    pets = pet_store.load()
    summary = timeline.cross_camera_summary(since_hours=24)
    return {
        "pets": [
            {
                "pet_id": p.pet_id,
                "name": p.name,
                "species": p.species,
                "notes": p.notes,
                "photo_count": len(p.photos),
                "photos": [
                    {"filename": ph.filename, "uploaded_at": ph.uploaded_at}
                    for ph in p.photos
                ],
                "stats": summary.get(p.pet_id, {"sightings": 0, "last_seen": 0, "cameras": []}),
            }
            for p in pets
        ],
        "recognition_ready": embeddings.model_path().exists(),
    }


@app.post("/api/pets")
async def api_pets_create(request: Request, payload: dict):
    _require_auth(request)
    name = (payload.get("name") or "").strip()
    species = (payload.get("species") or "cat").strip().lower()
    notes = (payload.get("notes") or "").strip()
    try:
        pet = pet_store.create(name=name, species=species, notes=notes)
    except pets_store.PetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "pet": pet.to_dict()}


@app.put("/api/pets/{pet_id}")
async def api_pets_update(request: Request, pet_id: str, payload: dict):
    _require_auth(request)
    try:
        pet = pet_store.update(
            pet_id,
            name=payload.get("name"),
            species=payload.get("species"),
            notes=payload.get("notes"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="pet not found")
    except pets_store.PetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "pet": pet.to_dict()}


@app.delete("/api/pets/{pet_id}")
async def api_pets_delete(request: Request, pet_id: str):
    _require_auth(request)
    if not pet_store.delete(pet_id):
        raise HTTPException(status_code=404, detail="pet not found")
    return {"ok": True}


@app.post("/api/pets/{pet_id}/photos")
async def api_pets_add_photo(request: Request, pet_id: str,
                              file: UploadFile = File(...)):
    """Upload one reference photo, embed it, persist to pets.yml."""
    _require_auth(request)
    pet = pet_store.get(pet_id)
    if not pet:
        raise HTTPException(status_code=404, detail="pet not found")
    from .utils import UploadTooLarge, read_capped_upload
    try:
        blob = await read_capped_upload(file, 10 * 1024 * 1024)  # 10 MB cap
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail="photo too large (max 10 MB)")
    if not blob:
        raise HTTPException(status_code=400, detail="empty file")

    extractor = embeddings.get_extractor()
    embed_result = extractor.extract(blob)
    if not embed_result.success:
        # Common case: model not downloaded yet. Tell the user clearly.
        raise HTTPException(
            status_code=503,
            detail=f"recognition model unavailable ({embed_result.error}). "
                   f"Run /api/pets/setup-model first."
        )
    # Pick extension based on uploaded file type, default .jpg.
    ext = ".jpg"
    if file.filename and "." in file.filename:
        suffix = "." + file.filename.rsplit(".", 1)[-1].lower()
        if suffix in (".jpg", ".jpeg", ".png", ".webp"):
            ext = suffix

    try:
        photo = pet_store.add_photo(
            pet_id, blob, embed_result.vector.tolist(),
            ext=ext, uploaded_at=int(time.time()),
        )
    except pets_store.PetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="pet not found")
    return {"ok": True, "photo": {"filename": photo.filename}}


@app.delete("/api/pets/{pet_id}/photos/{filename}")
async def api_pets_delete_photo(request: Request, pet_id: str, filename: str):
    _require_auth(request)
    if not pet_store.remove_photo(pet_id, filename):
        raise HTTPException(status_code=404, detail="photo not found")
    return {"ok": True}


@app.get("/api/pets/{pet_id}/photos/{filename}")
async def api_pets_get_photo(request: Request, pet_id: str, filename: str):
    _require_auth(request)
    path = pet_store.photo_path(pet_id, filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="photo not found")
    # Lightweight content-type — we only ever store jpeg/png/webp.
    suffix = path.suffix.lower()
    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
             "png": "image/png", "webp": "image/webp"}.get(suffix.lstrip("."), "application/octet-stream")
    return Response(content=path.read_bytes(), media_type=media,
                    headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/pets/{pet_id}/timeline")
async def api_pets_timeline(request: Request, pet_id: str, hours: float = 48.0):
    _require_auth(request)
    if not pet_store.get(pet_id):
        raise HTTPException(status_code=404, detail="pet not found")
    journeys = timeline.journeys_for_pet(pet_id, since_hours=float(hours))
    return {"journeys": [j.to_dict() for j in journeys]}


@app.post("/api/pets/backfill")
async def api_pets_backfill(request: Request, payload: dict | None = None):
    """Kick off a re-run of recognition over recent past events.
    Body: {"hours": 168} (defaults to 7 days). Returns immediately;
    poll /api/pets/backfill/progress for status."""
    _require_auth(request)
    payload = payload or {}
    hours = float(payload.get("hours") or 168.0)
    if recognition_backfill.current_progress().running:
        raise HTTPException(status_code=409, detail="a backfill is already running")
    # Schedule as a fire-and-forget task on the running event loop.
    asyncio.create_task(recognition_backfill.run_backfill(since_hours=hours))
    return {"ok": True, "hours": hours}


@app.get("/api/pets/backfill/progress")
async def api_pets_backfill_progress(request: Request):
    _require_auth(request)
    return recognition_backfill.current_progress().to_dict()


@app.post("/api/pets/backfill/pro")
async def api_pets_backfill_pro(request: Request, payload: dict | None = None):
    """Pro-tier backfill: up to 30 days + anomaly highlighting. Returns
    409 if the OSS backfill is already in flight (they share state).
    Returns 503 on OSS builds where the Pro module isn't installed."""
    _require_auth(request)
    if recognition_backfill_pro is None:
        raise HTTPException(status_code=503, detail="pro_backfill_unavailable")
    payload = payload or {}
    hours = float(payload.get("hours") or recognition_backfill_pro.MAX_HOURS_PRO)
    if recognition_backfill.current_progress().running:
        raise HTTPException(status_code=409, detail="a backfill is already running")
    asyncio.create_task(recognition_backfill_pro.run_backfill_pro(since_hours=hours))
    return {"ok": True, "hours": hours}


@app.get("/api/pets/backfill/pro/progress")
async def api_pets_backfill_pro_progress(request: Request):
    """Two booleans that the UI cares about:

    * `available` — Pro module is installed (drives "show 30-day vs 7-day")
    * `licensed`  — license key is configured (drives the upgrade-prompt
      gate). A Pro install with no license is otherwise indistinguishable
      from a paid one and would silently hide both the prompt AND the
      working features.
    """
    _require_auth(request)
    cfg = config_store.load_config()
    licensed = bool(cfg.pawcorder_pro_license_key)
    if recognition_backfill_pro is None:
        return {"available": False, "licensed": licensed}
    return {
        "available": True,
        "licensed": licensed,
        **recognition_backfill_pro.current_progress().to_dict(),
    }


@app.post("/api/pets/setup-model")
async def api_pets_setup_model(request: Request):
    """Download the embedding model on demand. Idempotent — already-
    present models return ok immediately."""
    _require_auth(request)
    ok = embeddings.download_model()
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="model download failed; check the host's internet access "
                   "or set PAWCORDER_EMBEDDING_MODEL_URL to a mirror"
        )
    return {"ok": True, "model_path": str(embeddings.model_path())}


# ---- backup / restore ---------------------------------------------------

@app.get("/api/backup/download")
async def api_backup_download(request: Request):
    """Stream a tar.gz of the user's config (env, cameras, rclone)."""
    _require_auth(request)
    blob = backup_mod.make_backup()
    fname = time.strftime("pawcorder-backup-%Y%m%d-%H%M%S.tar.gz")
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length": str(len(blob)),
        },
    )


@app.post("/api/backup/inspect")
async def api_backup_inspect(request: Request, file: UploadFile = File(...)):
    """Show what's in an uploaded backup file before the user commits to restore."""
    _require_auth(request)
    from .utils import UploadTooLarge, read_capped_upload
    try:
        blob = await read_capped_upload(file, 50 * 1024 * 1024)  # backups are tiny
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail="backup too large")
    try:
        meta = backup_mod.inspect_backup(blob)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return meta


@app.post("/api/backup/restore")
async def api_backup_restore(request: Request, file: UploadFile = File(...)):
    """Replace .env / cameras / rclone.conf from an uploaded backup.

    Caller is expected to stop pawcorder before doing this on a real
    deploy; we only validate paths and write atomically.
    """
    _require_auth(request)
    from .utils import UploadTooLarge, read_capped_upload
    try:
        blob = await read_capped_upload(file, 50 * 1024 * 1024)
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail="backup too large")
    result = backup_mod.restore_backup(blob)
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.error)
    return {"ok": True, "files_restored": result.files_restored}


# ---- updates ------------------------------------------------------------

@app.get("/api/system/version")
async def api_system_version(request: Request):
    _require_auth(request)
    return {"version": updater.current_version()}


@app.get("/docs/api", response_class=HTMLResponse)
async def docs_api_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    return _render("docs_api.html", request)


@app.get("/api/system/integrations", response_class=Response)
async def api_system_integrations(request: Request):
    """Serve docs/INTEGRATIONS.md as text. Auth required because it
    enumerates every endpoint."""
    _require_auth(request)
    p = Path("/data/INTEGRATIONS.md")
    if not p.exists():
        p = APP_ROOT.parent.parent / "docs" / "INTEGRATIONS.md"
    if not p.exists():
        return Response("INTEGRATIONS.md not bundled with this build.\n",
                        media_type="text/plain", status_code=404)
    return Response(p.read_text(encoding="utf-8"),
                    media_type="text/markdown; charset=utf-8")


@app.get("/api/system/notices", response_class=Response)
async def api_system_notices(request: Request):
    """Serve NOTICES.md (OSS acknowledgements) as plain text.

    Public-readable on purpose — license law usually requires
    acknowledgements be discoverable, and there's nothing sensitive
    in the file. The path is auth-free so even logged-out users can
    inspect compliance.
    """
    notices = Path("/data/NOTICES.md")
    if not notices.exists():
        # Fallback for dev / docker-less setups: relative to the repo root.
        notices = APP_ROOT.parent.parent / "NOTICES.md"
    if not notices.exists():
        return Response(content="NOTICES.md not bundled with this build.\n",
                        media_type="text/plain", status_code=404)
    return Response(content=notices.read_text(encoding="utf-8"),
                    media_type="text/markdown; charset=utf-8")


@app.get("/api/system/update-check")
async def api_update_check(request: Request, force: bool = False):
    _require_auth(request)
    result = await updater.check_for_updates(force=force)
    payload = result.to_dict()
    # The dashboard uses skipped_version to suppress the banner for one
    # specific release the user explicitly dismissed.
    payload["skipped_version"] = updater.load_skipped_version()
    payload["banner_visible"] = bool(
        result.update_available
        and result.latest_version
        and result.latest_version != payload["skipped_version"]
    )
    return payload


@app.post("/api/system/update-skip")
async def api_update_skip(request: Request, payload: dict):
    """Hide the dashboard banner for a specific release tag. Empty
    `version` clears the skip (re-show the banner)."""
    _require_auth(request)
    tag = (payload.get("version") or "").strip()
    updater.save_skipped_version(tag)
    return {"ok": True, "skipped_version": tag}


@app.get("/api/connect/status")
async def api_connect_status(request: Request):
    """Return current Connect-client state. tunnel_token is masked —
    only `has_tunnel_token: bool` leaks. Returns
    `{"available": false}` on OSS builds."""
    _require_auth(request)
    if connect_client is None:
        return {"available": False}
    return {"available": True, **connect_client.public_status()}


@app.post("/api/connect/register")
async def api_connect_register(request: Request, payload: dict | None = None):
    """Manually trigger a Connect registration. The background
    registrar runs every 12h; this is for immediate use after pasting
    a license. Body: `{"desired_subdomain": "..."}` (optional)."""
    _require_role(request, min_role="admin")
    if connect_client is None:
        raise HTTPException(status_code=503, detail="connect_unavailable")
    desired = (payload or {}).get("desired_subdomain")
    try:
        status = await connect_client.register(desired_subdomain=desired)
    except connect_client.ConnectNotConfigured:
        raise HTTPException(status_code=400, detail="no_pro_license")
    return {
        "subdomain": status.subdomain,
        "enabled": status.enabled,
        "last_error": status.last_error,
    }


@app.get("/api/b2b/sites")
async def api_b2b_sites(request: Request):
    """List configured B2B sites (NOT their api_keys). Pre-aggregation —
    use /api/b2b/dashboard for the actual snapshot pull."""
    _require_role(request, min_role="admin")
    if b2b_dashboard is None:
        return {"available": False, "sites": []}
    sites = b2b_dashboard.load_sites()
    return {
        "available": True,
        "sites": [{"name": s.name, "base_url": s.base_url} for s in sites],
    }


@app.get("/api/b2b/dashboard")
async def api_b2b_dashboard(request: Request):
    """Aggregate snapshots across every configured site. One slow site
    can't block the others — every fetch runs concurrently with a
    bounded timeout."""
    _require_role(request, min_role="admin")
    if b2b_dashboard is None:
        raise HTTPException(status_code=503, detail="b2b_unavailable")
    snapshots = await b2b_dashboard.aggregate()
    return {"sites": [s.to_dict() for s in snapshots]}


# Process-local lock for the OTA apply path. The deploy guide
# prescribes a single uvicorn worker, so this is sufficient. If someone
# scales out with `--workers N`, this guard becomes per-worker — the
# right replacement is a file-based lock (e.g. fcntl.flock on a path
# under DATA_DIR/config/) so all workers see the same in-flight state.
_update_apply_lock = asyncio.Lock()


@app.post("/api/system/update-apply")
async def api_update_apply(request: Request):
    """Run docker compose pull && up. Returns immediately with an
    "applying" status — the host-level orchestration will recreate
    the admin container partway through, so the client should poll
    /api/status until reachable again. Locked so two concurrent
    clicks don't double the pull bandwidth (compose's `up -d` is
    idempotent but `pull` isn't free)."""
    _require_role(request, min_role="admin")
    if _update_apply_lock.locked():
        return JSONResponse(
            status_code=409,
            content={"ok": False, "message": "already_applying",
                      "detail": "another update is in progress"},
        )
    async with _update_apply_lock:
        outcome = await updater.apply_update_compose()
    code = 200 if outcome.ok else 502
    return JSONResponse(
        status_code=code,
        content={"ok": outcome.ok, "message": outcome.message,
                  "detail": outcome.detail},
    )


# ---- health -------------------------------------------------------------

# Webhook dedup: Frigate may post the SAME event_id multiple times
# (new / update / end), and the polling loop will also see them. We
# keep a small bounded set of recently-handled event_ids so we only
# fire notifications once per event.
_webhook_handled_events: dict[str, float] = {}
_WEBHOOK_DEDUP_WINDOW_SECONDS = 600   # 10 min — cleared via the cap below
_WEBHOOK_DEDUP_MAX = 1024


def _seen_recently(event_id: str) -> bool:
    """Idempotent check + record. Trims the dict when it grows."""
    now = time.time()
    if event_id in _webhook_handled_events:
        return True
    _webhook_handled_events[event_id] = now
    if len(_webhook_handled_events) > _WEBHOOK_DEDUP_MAX:
        # Evict everything older than the dedup window.
        cutoff = now - _WEBHOOK_DEDUP_WINDOW_SECONDS
        for k in [k for k, v in _webhook_handled_events.items() if v < cutoff]:
            _webhook_handled_events.pop(k, None)
    return False


@app.post("/api/frigate/event")
async def api_frigate_event(request: Request, payload: dict):
    """Webhook receiver for Frigate's review/event hooks.

    Frigate can be configured to POST event JSON here on event
    creation/update/end. We do the same work the polling loop does
    (recognise pet, log sighting, push to Telegram / LINE / WebPush)
    but with sub-second latency instead of the 8 s poll cycle.

    No auth — Frigate runs on the same docker network and posts
    plaintext. The endpoint is bound only to localhost / docker net
    in production via Frigate's config; we accept any caller and
    rely on event_id uniqueness + idempotent deduping. If you really
    care, gate by source IP via reverse proxy.

    Soft-fails everything — webhook delivery is best-effort, the
    polling loop is still our backstop.
    """
    event_type = payload.get("type") or "new"
    after = payload.get("after") or payload.get("event") or {}
    if not isinstance(after, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    event_id = after.get("id")
    if not event_id:
        return {"ok": True, "skipped": "no event id"}
    if event_type not in ("new", "update", "end"):
        return {"ok": True, "skipped": f"event type {event_type!r} not handled"}
    if _seen_recently(str(event_id)):
        # Frigate posts new + update + end for the same event; the
        # polling loop also sees it. Notify once.
        return {"ok": True, "skipped": "duplicate"}

    cfg = config_store.load_config()
    telegram_on = bool(cfg.telegram_enabled and cfg.telegram_bot_token and cfg.telegram_chat_id)
    line_on = bool(cfg.line_enabled and cfg.line_channel_token and cfg.line_target_id)
    try:
        await tg.poller._notify(  # noqa: SLF001 — intentional shared path
            cfg, after, telegram_on=telegram_on, line_on=line_on,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(exc)})
    # Bump the poller's checkpoint so the polling loop doesn't
    # also notify when it next ticks.
    start_time = float(after.get("start_time") or 0)
    if start_time and start_time > tg.poller._last_seen:  # noqa: SLF001
        tg.poller._last_seen = start_time  # noqa: SLF001
    return {"ok": True}


@app.get("/api/backup/schedule")
async def api_backup_schedule_get(request: Request):
    _require_auth(request)
    return backup_schedule.load_state().to_dict()


@app.post("/api/backup/schedule")
async def api_backup_schedule_save(request: Request, payload: dict):
    _require_auth(request)
    state = backup_schedule.load_state()
    if "enabled" in payload:
        state.enabled = bool(payload.get("enabled"))
    if "encrypt" in payload:
        state.encrypt = bool(payload.get("encrypt"))
    if "encryption_password" in payload:
        # Only update if non-empty so editing other fields doesn't
        # accidentally wipe the password.
        new_pw = str(payload.get("encryption_password") or "")
        if new_pw:
            state.encryption_password = new_pw
    if payload.get("clear_password"):
        state.encryption_password = ""
    if "cloud_path" in payload:
        state.cloud_path = str(payload.get("cloud_path") or "pawcorder/backups")
    backup_schedule.save_state(state)
    return state.to_dict()


@app.post("/api/backup/run-now")
async def api_backup_run_now(request: Request):
    """Manually trigger a backup-to-cloud right now. Useful for testing
    + 'I'm about to nuke my host, push a fresh one first'."""
    _require_auth(request)
    return await backup_schedule.run_once_now()


@app.get("/api/webpush/public-key")
async def api_webpush_public_key(request: Request):
    """The VAPID public key the browser needs for PushManager.subscribe.
    Auth-required because subscribing without consent of the admin
    is meaningless."""
    _require_auth(request)
    return {"public_key": webpush.public_key_b64()}


@app.post("/api/webpush/subscribe")
async def api_webpush_subscribe(request: Request, payload: dict):
    """Browser POSTs the PushSubscription JSON here after consent."""
    _require_auth(request)
    sub = payload.get("subscription") or {}
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(status_code=400, detail="invalid subscription")
    rec = webpush.add_subscription(
        endpoint=endpoint,
        p256dh=str(keys.get("p256dh")),
        auth=str(keys.get("auth")),
        user_agent=request.headers.get("user-agent", ""),
    )
    return {"ok": True, "endpoint": rec.endpoint}


@app.delete("/api/webpush/subscribe")
async def api_webpush_unsubscribe(request: Request, payload: dict):
    _require_auth(request)
    endpoint = (payload.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail="endpoint required")
    webpush.remove_subscription(endpoint)
    return {"ok": True}


@app.post("/api/webpush/test")
async def api_webpush_test(request: Request):
    """Fire a 'test' push to every subscriber — proves the loop end-to-end."""
    _require_auth(request)
    return webpush.send_to_all(
        title="pawcorder",
        body="Test push — your subscription is working.",
        url="/",
    )


@app.post("/api/webpush/native")
async def api_webpush_native(request: Request, payload: dict):
    """Register a native APNs / FCM token from the Capacitor mobile shell.

    The native app posts ``{token, platform}`` after asking for push
    permission on first launch. We dedupe by token value so a phone
    re-registering on every launch (iOS does this) is idempotent.

    The ``webpush`` module already stores native tokens alongside its
    Web Push subscriptions; this just forwards the call. The actual
    push dispatch path lives in ``webpush.send_to_all`` which routes
    APNs / FCM / VAPID by token shape.
    """
    _require_auth(request)
    token = (payload.get("token") or "").strip()
    platform = (payload.get("platform") or "").strip().lower()
    if not token:
        raise HTTPException(status_code=400, detail="token_required")
    if platform not in ("ios", "android"):
        raise HTTPException(status_code=400, detail="bad_platform")
    try:
        return webpush.add_native_token(token=token, platform=platform)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not auth.is_authenticated(request):
        return _redirect_login()
    actual = users.role_from_request(request) or "admin"
    if actual != "admin":
        return _render_html_error(request, 403, "user management is admin-only")
    return _render(
        "users.html", request,
        users=[u.to_public() for u in users.list_users()],
        legacy_mode=not users.has_users(),
        roles=users.ROLES,
        current_user=users.session_username(request) if hasattr(users, "session_username") else None,
    )


@app.get("/api/users")
async def api_users_list(request: Request):
    _require_role(request, min_role="admin")
    return {
        "users": [u.to_public() for u in users.list_users()],
        "legacy_mode": not users.has_users(),
        "roles": list(users.ROLES),
    }


@app.post("/api/users")
async def api_users_create(request: Request, payload: dict):
    _require_role(request, min_role="admin")
    try:
        u = users.create_user(
            username=str(payload.get("username") or ""),
            password=str(payload.get("password") or ""),
            role=str(payload.get("role") or "family"),
        )
    except users.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "user": u.to_public()}


@app.put("/api/users/{username}")
async def api_users_update(request: Request, username: str, payload: dict):
    _require_role(request, min_role="admin")
    try:
        if "role" in payload:
            users.change_role(username, str(payload.get("role") or ""))
        if "password" in payload and payload.get("password"):
            users.change_password(username, str(payload.get("password")))
    except users.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    u = users.get_user(username)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True, "user": u.to_public()}


@app.delete("/api/users/{username}")
async def api_users_delete(request: Request, username: str):
    _require_role(request, min_role="admin")
    try:
        ok = users.delete_user(username)
    except users.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@app.get("/api/users/me")
async def api_users_me(request: Request):
    """Current session's username + role — UI uses this to grey out
    admin-only buttons for family/kid users."""
    _require_auth(request)
    actual = users.role_from_request(request) or "admin"
    payload = auth.session_payload(request) or {}
    return {
        "username": payload.get("username") or "(legacy)",
        "role": actual,
    }


@app.get("/api/system/api-keys")
async def api_system_keys_list(request: Request):
    """List all API keys (public view — never includes the hash)."""
    _require_auth(request)
    return {"keys": api_keys.list_keys_public()}


@app.post("/api/system/api-keys")
async def api_system_keys_create(request: Request, payload: dict):
    """Mint a new API key. The plain `key` is returned ONCE in the
    response — the user must save it; the server keeps only the hash."""
    _require_auth(request)
    name = (payload.get("name") or "").strip()
    plain, record = api_keys.create_key(name)
    return {
        "ok": True,
        "key": plain,           # show once
        "record": record.to_public_dict(),
    }


@app.delete("/api/system/api-keys/{key_id}")
async def api_system_keys_revoke(request: Request, key_id: str):
    _require_auth(request)
    if not api_keys.revoke_key(key_id):
        raise HTTPException(status_code=404, detail="key not found")
    return {"ok": True}


@app.get("/api/system/perf")
async def api_system_perf(request: Request):
    """Live CPU / RAM / network per container — for the /system perf
    panel. ~50 ms call total for our three containers; safe to poll
    every 5 s from the UI."""
    _require_auth(request)
    return {"snapshots": [s.to_dict() for s in perf.snapshot_all()]}


@app.get("/api/system/health")
async def api_system_health(request: Request):
    """Live snapshot for the /system page. Falls back to a fresh probe
    if the background monitor hasn't run yet (e.g. immediately after start)."""
    _require_auth(request)
    snap = health.monitor.current() or await health.snapshot()
    return snap.to_dict()


# ---- privacy mode -------------------------------------------------------

@app.get("/api/privacy")
async def api_privacy_get(request: Request):
    _require_auth(request)
    state = privacy.load_state()
    return state.to_dict()


@app.post("/api/privacy")
async def api_privacy_save(request: Request, payload: dict):
    """Update privacy settings. Body fields:
        enabled: bool
        auto_pause_when_home: bool
        paused_now: bool         (only honored when auto-mode is OFF)
        home_devices: list[str]
    """
    _require_auth(request)
    state = privacy.load_state()
    if "enabled" in payload:
        state.enabled = bool(payload.get("enabled"))
    if "auto_pause_when_home" in payload:
        state.auto_pause_when_home = bool(payload.get("auto_pause_when_home"))
    if "paused_now" in payload and not state.auto_pause_when_home:
        # Manual override — only honored when auto mode is off, otherwise
        # the next evaluate_async() would clobber it instantly.
        state.paused_now = bool(payload.get("paused_now"))
    if "home_devices" in payload:
        devs = payload.get("home_devices") or []
        if not isinstance(devs, list):
            raise HTTPException(status_code=400, detail="home_devices must be a list")
        state.home_devices = [str(d).strip() for d in devs if str(d).strip()]
    privacy.save_state(state)
    return state.to_dict()


@app.post("/api/privacy/evaluate")
async def api_privacy_evaluate(request: Request):
    """Re-check Tailscale presence right now and return the updated state.
    Used by the UI's 'check now' button."""
    _require_auth(request)
    state = await privacy.evaluate_async()
    privacy.save_state(state)
    return state.to_dict()


# ---- uninstall ---------------------------------------------------------

@app.get("/api/uninstall/inventory")
async def api_uninstall_inventory(request: Request):
    """List every path/container pawcorder owns + their sizes. UI uses
    this to show "your recordings take 47 GB" before the user commits
    to a destructive action."""
    _require_auth(request)
    return uninstall_mod.take_inventory().to_dict()


@app.post("/api/uninstall/reset")
async def api_uninstall_reset(request: Request):
    """The soft path — wipe per-feature config but keep the admin
    password and recordings. Admin keeps running. Setup wizard appears
    on next page load."""
    _require_auth(request)
    result = uninstall_mod.reset_app_data()
    if not result.ok:
        raise HTTPException(status_code=500, detail=result.error)
    return {
        "ok": True,
        "removed": result.removed,
        "skipped": result.skipped,
    }


@app.get("/api/uninstall/command")
async def api_uninstall_command(request: Request, level: str = "full",
                                 project_dir: str = "~/pawcorder"):
    """Generate the shell command for the level the user picked.

    We deliberately don't execute this from inside the admin — once we
    docker-compose-down the admin's own container, the request hangs.
    Honest UX: print the command, let the user paste it on the host.
    """
    _require_auth(request)
    if level not in ("soft", "full", "nuke"):
        raise HTTPException(status_code=400, detail="level must be soft / full / nuke")
    return {
        "level": level,
        "command": uninstall_mod.uninstall_command(level, project_dir=project_dir),
    }


# ---- error handling ------------------------------------------------------

# Friendly explainer text per status — displayed on the html error page.
# Keys are HTTP status codes; values are i18n keys resolved at render time.
_HTML_ERROR_EXPLANATIONS = {
    404: "ERR_PAGE_NOT_FOUND",
    500: "ERR_SERVER_BROKE",
    502: "ERR_UPSTREAM_DOWN",
    503: "ERR_UNAVAILABLE",
}


def _render_html_error(request: Request, status: int, detail: str) -> HTMLResponse:
    """Tiny self-contained error page — no template lookup so the
    handler still works when the template engine itself is the problem."""
    lang = i18n.get_lang_from_request(request)
    t = i18n.make_translator(lang)
    title_key = _HTML_ERROR_EXPLANATIONS.get(status, "ERR_GENERIC")
    body_key = title_key + "_BODY"
    title = t(title_key)
    body = t(body_key)
    home_label = t("ERR_BACK_HOME")
    html = f"""<!doctype html>
<html lang="{lang}" class="h-full">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>pawcorder — {status}</title>
<script>
(function(){{var t=localStorage.getItem('pawcorder_theme')||'auto';
var d=t==='dark'||(t==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches);
if(d)document.documentElement.classList.add('dark');}})();
</script>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={{darkMode:'class'}};</script>
<style>body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}</style>
</head>
<body class="bg-slate-50 dark:bg-slate-950 text-slate-800 dark:text-slate-200 min-h-screen flex items-center justify-center p-6">
  <div class="max-w-md text-center">
    <p class="text-7xl font-bold text-slate-300 dark:text-slate-700">{status}</p>
    <h1 class="mt-2 text-xl font-semibold tracking-tight">{title}</h1>
    <p class="mt-3 text-sm text-slate-500 dark:text-slate-400">{body}</p>
    <p class="mt-1 text-xs text-slate-400 dark:text-slate-500 font-mono">{detail}</p>
    <a href="/" class="mt-6 inline-flex items-center rounded-md bg-slate-900 hover:bg-slate-800 dark:bg-slate-100 dark:hover:bg-white text-white dark:text-slate-900 px-4 py-2 text-sm font-medium">{home_label} →</a>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=status)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": exc.detail})
        return _redirect_login()
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    # HTML pages: render a friendly error page rather than letting
    # Starlette show the default uvicorn stack-trace look.
    return _render_html_error(request, exc.status_code, str(exc.detail or ""))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler for unexpected failures. Always logs the
    full traceback server-side; users see a friendly 500 page."""
    import logging, traceback
    logging.getLogger("pawcorder").error(
        "unhandled exception on %s %s\n%s",
        request.method, request.url.path, traceback.format_exc(),
    )
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=500, content={"error": "internal error"})
    return _render_html_error(request, 500, "internal error")
