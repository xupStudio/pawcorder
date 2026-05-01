"""Read/write .env, render Frigate config from a Jinja2 template.

Per-camera config now lives in config/cameras.yml (see cameras_store.py).
.env keeps host-wide knobs only: admin/session secrets, storage path,
detection thresholds, timezone.

  /data/.env                          host-wide settings
  /data/config/cameras.yml            list of cameras (managed by admin)
  /data/config/frigate.template.yml   Jinja2 template
  /data/config/frigate.yml            rendered output, consumed by Frigate
"""
from __future__ import annotations

import os
import re
import secrets
import string
from dataclasses import dataclass
from pathlib import Path

import jinja2

from .cameras_store import Camera, CameraStore

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
ENV_PATH = DATA_DIR / ".env"
TEMPLATE_PATH = DATA_DIR / "config" / "frigate.template.yml"
# Frigate reads its config from /config/config.yml (a hard-coded path inside
# the container). The directory bind-mount at ./config:/config means we
# render to ./config/config.yml on the host.
RENDERED_PATH = DATA_DIR / "config" / "config.yml"

DEFAULTS: dict[str, str] = {
    "STORAGE_PATH": "/mnt/pawcorder",
    "FRIGATE_RTSP_PASSWORD": "",
    "TZ": "Asia/Taipei",
    "PET_MIN_SCORE": "0.65",
    "PET_THRESHOLD": "0.70",
    "ADMIN_PASSWORD": "",
    "ADMIN_SESSION_SECRET": "",
    "TAILSCALE_HOSTNAME": "",
    "TELEGRAM_ENABLED": "0",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "LINE_ENABLED": "0",
    "LINE_CHANNEL_TOKEN": "",
    "LINE_TARGET_ID": "",
    "ADMIN_LANG": "zh-TW",
    "TRACK_CAT": "1",
    "TRACK_DOG": "1",
    "TRACK_PERSON": "1",
    # Frigate detector type. Auto-detected at install time by platform_detect;
    # user can override on the /hardware admin page. Valid values are listed
    # in app.platform_detect.VALID_DETECTORS.
    "DETECTOR_TYPE": "cpu",
    # Cloud backup (rclone). Backend-specific credentials live in
    # config/rclone/rclone.conf, not here.
    "CLOUD_ENABLED": "0",
    "CLOUD_BACKEND": "",
    "CLOUD_REMOTE_NAME": "pawcorder",
    "CLOUD_REMOTE_PATH": "pawcorder",
    "CLOUD_UPLOAD_ONLY_PETS": "1",
    "CLOUD_UPLOAD_MIN_SCORE": "0.75",
    "CLOUD_RETENTION_DAYS": "90",
    "CLOUD_MAX_SIZE_GB": "0",          # 0 = no cap
    "CLOUD_SIZE_MODE": "manual",       # 'manual' or 'adaptive'
    "CLOUD_ADAPTIVE_FRACTION": "0.80", # used when mode == 'adaptive'
    # AI tokens (System page). OPENAI_API_KEY = OSS LLM diary path
    # (user pays OpenAI directly). PAWCORDER_PRO_LICENSE_KEY = managed
    # path (we proxy LLM + own the bill). OLLAMA_BASE_URL = local
    # offline LLM (no cloud, no token cost — runs on the user's host).
    "OPENAI_API_KEY": "",
    "PAWCORDER_PRO_LICENSE_KEY": "",
    "OLLAMA_BASE_URL": "",
    "OLLAMA_MODEL": "qwen2.5:3b",
    # Multi-provider LLM support — bring-your-own-key for direct vendor
    # access, or fall through to Pro relay. Default empty = provider not
    # configured; LLM dispatcher silently skips it.
    "GEMINI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    # "auto" preserves the historical priority order
    # (ollama > openai > gemini > anthropic > pro_relay). An explicit
    # value pins the dispatcher to one vendor — useful when an operator
    # wants Claude Haiku 4.5 specifically for zh-TW prosody.
    "LLM_PROVIDER_PREFERENCE": "auto",
    "TTS_PROVIDER_PREFERENCE": "auto",
    "TTS_VOICE": "",
    "PAWCORDER_EMBEDDING_BACKBONE": "",
    # Federated baseline opt-in. Default OFF — explicit consent only.
    "FEDERATED_OPT_IN": "0",
    # Pro health detectors. Empty / 0-equivalent values disable.
    "LITTER_BOX_CAMERA": "",
    "LITTER_VISITS_ALERT_PER_24H": "12",
}

REQUIRED_FOR_FRIGATE = ("STORAGE_PATH", "FRIGATE_RTSP_PASSWORD")

_LINE_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


@dataclass
class Config:
    storage_path: str = "/mnt/pawcorder"
    frigate_rtsp_password: str = ""
    tz: str = "Asia/Taipei"
    pet_min_score: str = "0.65"
    pet_threshold: str = "0.70"
    admin_password: str = ""
    admin_session_secret: str = ""
    tailscale_hostname: str = ""
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    line_enabled: bool = False
    line_channel_token: str = ""
    line_target_id: str = ""
    admin_lang: str = "zh-TW"
    track_cat: bool = True
    track_dog: bool = True
    track_person: bool = True
    detector_type: str = "cpu"
    cloud_enabled: bool = False
    cloud_backend: str = ""
    cloud_remote_name: str = "pawcorder"
    cloud_remote_path: str = "pawcorder"
    cloud_upload_only_pets: bool = True
    cloud_upload_min_score: str = "0.75"
    cloud_retention_days: str = "90"
    cloud_max_size_gb: str = "0"
    cloud_size_mode: str = "manual"
    cloud_adaptive_fraction: str = "0.80"
    # OSS LLM diary path: bring-your-own OpenAI key.
    # Pro path: a license key is exchanged for a relay token at the
    # pawcorder cloud; the admin then makes LLM calls via that relay
    # without ever holding the user's OpenAI credentials.
    # Offline path: local Ollama (or any OpenAI-compatible local
    # server) reachable at ``ollama_base_url``. Wins over the cloud
    # backends when set, because the user explicitly opted into local.
    openai_api_key: str = ""
    pawcorder_pro_license_key: str = ""
    ollama_base_url: str = ""
    ollama_model: str = "qwen2.5:3b"
    # Additional cloud LLM providers (mid-2026 best-in-class options).
    # Bring-your-own-key, same shape as openai_api_key — admin holds
    # nothing the user wouldn't already have to acquire from the vendor.
    # Empty string disables that provider; the dispatcher then ignores
    # it during selection.
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    # Preferred LLM provider for the diary / Q&A path. Values:
    # "auto" picks the first configured in priority order
    # (ollama → openai → gemini → anthropic → pro_relay), matching the
    # historical default. Operators set this when they want to pin a
    # specific vendor (e.g. "anthropic" for zh-TW prosody).
    llm_provider_preference: str = "auto"
    # TTS provider for the weekly podcast / digest. "auto" lets the
    # admin pick whatever the relay supports; explicit values forward to
    # the matching adapter on the relay side.
    tts_provider_preference: str = "auto"
    tts_voice: str = ""
    # Recognition embedding backbone. "" = use whatever
    # PAWCORDER_EMBEDDING_BACKBONE env or default points at; explicit
    # values override the env. Surfaced on System so the operator can
    # opt into DINOv2-small for better fine-grained features without
    # editing .env by hand. A change here without re-enroll leaves
    # photos in the old feature space — the /pets page surfaces a
    # "Re-enroll needed" badge in that case.
    embedding_backbone: str = ""
    # Opt-in toggle for federated cohort baselines (Pro-only).
    # Default false; user explicitly enables under System.
    federated_opt_in: bool = False
    # Pro health detectors. Empty values disable the detector — the
    # OSS build never reads them, but it's tidier to keep one canonical
    # config schema rather than scatter Pro-only fields elsewhere.
    litter_box_camera: str = ""           # camera that frames the box; "" disables
    litter_visits_alert_per_24h: str = "12"   # > N visits in 24h fires UTI alert

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Config":
        return cls(
            storage_path=env.get("STORAGE_PATH", "/mnt/pawcorder"),
            frigate_rtsp_password=env.get("FRIGATE_RTSP_PASSWORD", ""),
            tz=env.get("TZ", "Asia/Taipei"),
            pet_min_score=env.get("PET_MIN_SCORE", "0.65"),
            pet_threshold=env.get("PET_THRESHOLD", "0.70"),
            admin_password=env.get("ADMIN_PASSWORD", ""),
            admin_session_secret=env.get("ADMIN_SESSION_SECRET", ""),
            tailscale_hostname=env.get("TAILSCALE_HOSTNAME", ""),
            telegram_enabled=env.get("TELEGRAM_ENABLED", "0") in ("1", "true", "True"),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
            line_enabled=env.get("LINE_ENABLED", "0") in ("1", "true", "True"),
            line_channel_token=env.get("LINE_CHANNEL_TOKEN", ""),
            line_target_id=env.get("LINE_TARGET_ID", ""),
            admin_lang=env.get("ADMIN_LANG", "zh-TW"),
            track_cat=env.get("TRACK_CAT", "1") in ("1", "true", "True"),
            track_dog=env.get("TRACK_DOG", "1") in ("1", "true", "True"),
            track_person=env.get("TRACK_PERSON", "1") in ("1", "true", "True"),
            detector_type=env.get("DETECTOR_TYPE", "cpu"),
            cloud_enabled=env.get("CLOUD_ENABLED", "0") in ("1", "true", "True"),
            cloud_backend=env.get("CLOUD_BACKEND", ""),
            cloud_remote_name=env.get("CLOUD_REMOTE_NAME", "pawcorder"),
            cloud_remote_path=env.get("CLOUD_REMOTE_PATH", "pawcorder"),
            cloud_upload_only_pets=env.get("CLOUD_UPLOAD_ONLY_PETS", "1") in ("1", "true", "True"),
            cloud_upload_min_score=env.get("CLOUD_UPLOAD_MIN_SCORE", "0.75"),
            cloud_retention_days=env.get("CLOUD_RETENTION_DAYS", "90"),
            cloud_max_size_gb=env.get("CLOUD_MAX_SIZE_GB", "0"),
            cloud_size_mode=env.get("CLOUD_SIZE_MODE", "manual"),
            cloud_adaptive_fraction=env.get("CLOUD_ADAPTIVE_FRACTION", "0.80"),
            openai_api_key=env.get("OPENAI_API_KEY", ""),
            pawcorder_pro_license_key=env.get("PAWCORDER_PRO_LICENSE_KEY", ""),
            ollama_base_url=env.get("OLLAMA_BASE_URL", ""),
            ollama_model=env.get("OLLAMA_MODEL", "qwen2.5:3b"),
            gemini_api_key=env.get("GEMINI_API_KEY", ""),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
            llm_provider_preference=env.get("LLM_PROVIDER_PREFERENCE", "auto"),
            tts_provider_preference=env.get("TTS_PROVIDER_PREFERENCE", "auto"),
            tts_voice=env.get("TTS_VOICE", ""),
            embedding_backbone=env.get("PAWCORDER_EMBEDDING_BACKBONE", ""),
            federated_opt_in=env.get("FEDERATED_OPT_IN", "0") in ("1", "true", "True"),
            litter_box_camera=env.get("LITTER_BOX_CAMERA", ""),
            litter_visits_alert_per_24h=env.get("LITTER_VISITS_ALERT_PER_24H", "12"),
        )

    def to_env(self) -> dict[str, str]:
        return {
            "STORAGE_PATH": self.storage_path,
            "FRIGATE_RTSP_PASSWORD": self.frigate_rtsp_password,
            "TZ": self.tz,
            "PET_MIN_SCORE": self.pet_min_score,
            "PET_THRESHOLD": self.pet_threshold,
            "ADMIN_PASSWORD": self.admin_password,
            "ADMIN_SESSION_SECRET": self.admin_session_secret,
            "TAILSCALE_HOSTNAME": self.tailscale_hostname,
            "TELEGRAM_ENABLED": "1" if self.telegram_enabled else "0",
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
            "LINE_ENABLED": "1" if self.line_enabled else "0",
            "LINE_CHANNEL_TOKEN": self.line_channel_token,
            "LINE_TARGET_ID": self.line_target_id,
            "ADMIN_LANG": self.admin_lang,
            "TRACK_CAT": "1" if self.track_cat else "0",
            "TRACK_DOG": "1" if self.track_dog else "0",
            "TRACK_PERSON": "1" if self.track_person else "0",
            "DETECTOR_TYPE": self.detector_type,
            "CLOUD_ENABLED": "1" if self.cloud_enabled else "0",
            "CLOUD_BACKEND": self.cloud_backend,
            "CLOUD_REMOTE_NAME": self.cloud_remote_name,
            "CLOUD_REMOTE_PATH": self.cloud_remote_path,
            "CLOUD_UPLOAD_ONLY_PETS": "1" if self.cloud_upload_only_pets else "0",
            "CLOUD_UPLOAD_MIN_SCORE": self.cloud_upload_min_score,
            "CLOUD_RETENTION_DAYS": self.cloud_retention_days,
            "CLOUD_MAX_SIZE_GB": self.cloud_max_size_gb,
            "CLOUD_SIZE_MODE": self.cloud_size_mode,
            "CLOUD_ADAPTIVE_FRACTION": self.cloud_adaptive_fraction,
            "OPENAI_API_KEY": self.openai_api_key,
            "PAWCORDER_PRO_LICENSE_KEY": self.pawcorder_pro_license_key,
            "OLLAMA_BASE_URL": self.ollama_base_url,
            "OLLAMA_MODEL": self.ollama_model,
            "GEMINI_API_KEY": self.gemini_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "LLM_PROVIDER_PREFERENCE": self.llm_provider_preference,
            "TTS_PROVIDER_PREFERENCE": self.tts_provider_preference,
            "TTS_VOICE": self.tts_voice,
            "PAWCORDER_EMBEDDING_BACKBONE": self.embedding_backbone,
            "FEDERATED_OPT_IN": "1" if self.federated_opt_in else "0",
            "LITTER_BOX_CAMERA": self.litter_box_camera,
            "LITTER_VISITS_ALERT_PER_24H": self.litter_visits_alert_per_24h,
        }


def is_setup_complete(cfg: "Config", cameras: list[Camera]) -> bool:
    env = cfg.to_env()
    return all(env.get(k) for k in REQUIRED_FOR_FRIGATE) and len(cameras) > 0


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            quote = val[0]
            val = val[1:-1]
            if quote == '"':
                # Reverse the escapes applied in write_env. Use a sentinel to
                # avoid double-replacing when both backslash and quote are present.
                val = val.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
        out[key] = val
    return out


def write_env(env: dict[str, str]) -> None:
    """Atomic write — .env holds ADMIN_PASSWORD and ADMIN_SESSION_SECRET.
    A torn write here locks the user out of the admin panel entirely:
    existing session cookies fail to verify (secret gone) and login
    fails too (password gone), forcing manual SSH recovery.
    """
    from .utils import atomic_write_text

    lines = ["# pawcorder host-wide configuration. Managed by the admin panel.", ""]
    for key in DEFAULTS:
        val = env.get(key, DEFAULTS[key])
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    atomic_write_text(ENV_PATH, "\n".join(lines) + "\n")


def load_config() -> Config:
    return Config.from_env(read_env())


def save_config(cfg: Config) -> None:
    write_env(cfg.to_env())


def _jinja_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_PATH.parent)),
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
        undefined=jinja2.StrictUndefined,
    )
    return env


def render_frigate_config(cfg: Config, cameras: list[Camera]) -> str:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Frigate template not found at {TEMPLATE_PATH}")
    # Local imports to avoid circular load (privacy + insights would
    # otherwise pull config_store at module level).
    from . import insights, privacy
    recording_paused = privacy.is_paused()
    energy_mode = insights.load_energy_mode()

    # Augment each camera's template_view with an `energy_paused` flag
    # the template uses to flip enabled: false on idle hours.
    cam_views = []
    for cam in cameras:
        view = cam.template_view()
        view["energy_paused"] = insights.is_camera_currently_paused(
            cam.name, mode=energy_mode,
        )
        cam_views.append(view)

    template = _jinja_env().get_template(TEMPLATE_PATH.name)
    return template.render(
        cameras=cam_views,
        pet_min_score=cfg.pet_min_score,
        pet_threshold=cfg.pet_threshold,
        tz=cfg.tz,
        track_cat=cfg.track_cat,
        track_dog=cfg.track_dog,
        track_person=cfg.track_person,
        detector_type=cfg.detector_type,
        recording_paused=recording_paused,
    )


def write_frigate_config(cfg: Config, cameras: list[Camera]) -> Path:
    """Atomic write — Frigate reads this on every restart, and a half-
    written YAML kills it on parse. Same pattern as cameras_store.save."""
    from .utils import atomic_write_text

    rendered = render_frigate_config(cfg, cameras)
    atomic_write_text(RENDERED_PATH, rendered)
    return RENDERED_PATH


def render_and_write_if_complete(cfg: Config | None = None, cameras: list[Camera] | None = None) -> bool:
    """Render only when at least one camera is configured. Returns True if written."""
    cfg = cfg or load_config()
    cameras = cameras if cameras is not None else CameraStore().load()
    if not cameras:
        return False
    write_frigate_config(cfg, cameras)
    return True


def random_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def random_secret(length: int = 48) -> str:
    return secrets.token_urlsafe(length)
