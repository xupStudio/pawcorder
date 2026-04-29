"""Camera list persistence (config/cameras.yml).

Schema:
    cameras:
      - name: living_room   # unique slug, used as a Frigate camera key
        ip: 192.168.1.100
        user: admin
        password: secret
        rtsp_port: 554
        onvif_port: 8000
        detect_width: 640
        detect_height: 480
        enabled: true

The .env still holds host-wide settings (admin password, storage path,
detection thresholds, timezone) — anything that isn't per-camera.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import yaml

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
CAMERAS_PATH = DATA_DIR / "config" / "cameras.yml"

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def _path_only(rtsp_url: str) -> str:
    """Extract everything from the first slash after the host onwards.

    >>> _path_only("rtsp://USER:PASS@IP:554/stream1")
    '/stream1'
    >>> _path_only("rtsp://USER:PASS@IP:7447/AAA/BBB")
    '/AAA/BBB'
    """
    # rtsp://USER:PASS@IP:PORT/path → split off the scheme://authority
    if "://" not in rtsp_url:
        return rtsp_url
    after_scheme = rtsp_url.split("://", 1)[1]
    if "/" not in after_scheme:
        return "/"
    return "/" + after_scheme.split("/", 1)[1]


@dataclass
class Camera:
    name: str
    ip: str
    password: str
    user: str = "admin"
    rtsp_port: int = 554
    onvif_port: int = 8000
    detect_width: int = 640
    detect_height: int = 480
    enabled: bool = True
    # Auto-detected from Reolink GetLocalLink. Informational only — we
    # don't change the Frigate config based on this. One of:
    #   "wifi", "wired", "unknown"
    connection_type: str = "unknown"
    # Camera brand. Informational; helps the cameras page suggest the
    # right RTSP path for non-Reolink models. One of the keys in
    # camera_compat.BRANDS, or "other".
    brand: str = "reolink"
    # User flips this on if their camera supports talk-back over RTSP
    # (Reolink E1-series, Tapo C-series with backchannel, Amcrest most
    # models). Off by default — go2rtc only emits the backchannel stream
    # when this is enabled, otherwise we don't risk an extra failed dial.
    two_way_audio: bool = False
    # Audio detection — Frigate scans the audio track for tagged sounds
    # like 'bark', 'meow', 'glass_break'. The user opts in per camera
    # because the AI cost is small but non-zero per stream.
    audio_detection: bool = False
    # User-defined named zones. Each zone is a polygon (list of
    # [x, y] pairs in 0..1 normalized coords) plus a name like
    # "food_bowl" / "litter_box". Frigate uses these for dwell-time
    # tracking and zone-aware events. Stored as a list of dicts so the
    # YAML round-trip is clean.
    zones: list[dict] = field(default_factory=list)
    # Privacy mask polygons — Frigate blacks out these regions BEFORE
    # detection / record. Use case: bathroom corner of a hallway camera,
    # neighbour's window in your garden cam.
    privacy_masks: list[dict] = field(default_factory=list)
    # ONVIF PTZ presets — list of named pan/tilt positions the user
    # saved. Each entry: {"name": "feeding_spot", "preset_token": "1"}.
    # Frigate's UI saves these via ONVIF; we expose them so admin can
    # surface quick-jump buttons on the camera card.
    ptz_presets: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Camera":
        allowed = {f.name for f in fields(Camera)}
        return Camera(**{k: v for k, v in d.items() if k in allowed})

    def template_view(self) -> dict:
        """Return a dict the Jinja2 Frigate template can use safely.

        URL-encodes user/password so they're safe inside RTSP URLs even if
        they contain `:` `@` `/` etc. The bare values are also exposed for
        the ONVIF block (rendered with ``| tojson`` for YAML safety).

        Also injects brand-specific RTSP paths (rtsp_main_path /
        rtsp_sub_path) so the same template works for Reolink, Tapo,
        Hikvision, Dahua, etc. without per-brand template branches.
        """
        # Local import to keep cameras_store loadable without camera_compat
        # (e.g. during early bootstrap, or in narrow unit tests).
        from .camera_compat import BRANDS

        d = self.to_dict()
        d["user_url"] = quote(self.user, safe="")
        d["password_url"] = quote(self.password, safe="")
        spec = BRANDS.get(self.brand or "reolink", BRANDS["reolink"])
        # The path part (everything after host:port) — strip the
        # rtsp://USER:PASS@IP:PORT prefix from the brand template.
        d["rtsp_main_path"] = _path_only(spec.rtsp_main)
        d["rtsp_sub_path"] = _path_only(spec.rtsp_sub)
        return d


class CameraValidationError(ValueError):
    pass


def validate_name(name: str, *, existing_names: Iterable[str] = (), allow_existing: str | None = None) -> None:
    if not NAME_RE.match(name):
        raise CameraValidationError(
            "Camera name must start with a lowercase letter and contain only "
            "lowercase letters, digits, or underscores (max 31 characters)."
        )
    if name in existing_names and name != allow_existing:
        raise CameraValidationError(f"A camera named {name!r} already exists.")


def validate_camera(c: Camera, *, existing_names: Iterable[str] = (), allow_existing: str | None = None) -> None:
    validate_name(c.name, existing_names=existing_names, allow_existing=allow_existing)
    if not c.ip:
        raise CameraValidationError("Camera IP is required.")
    if not c.password:
        raise CameraValidationError("Camera password is required.")
    if not (1 <= int(c.rtsp_port) <= 65535):
        raise CameraValidationError("RTSP port must be between 1 and 65535.")
    if not (1 <= int(c.onvif_port) <= 65535):
        raise CameraValidationError("ONVIF port must be between 1 and 65535.")
    if not (160 <= int(c.detect_width) <= 1920):
        raise CameraValidationError("detect_width should be between 160 and 1920.")
    if not (120 <= int(c.detect_height) <= 1080):
        raise CameraValidationError("detect_height should be between 120 and 1080.")


class CameraStore:
    def __init__(self, path: Path = CAMERAS_PATH) -> None:
        self.path = path

    def load(self) -> list[Camera]:
        if not self.path.exists():
            return []
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return []
        raw_list = data.get("cameras") if isinstance(data, dict) else None
        if not isinstance(raw_list, list):
            return []
        out: list[Camera] = []
        for entry in raw_list:
            if isinstance(entry, dict) and entry.get("name"):
                try:
                    out.append(Camera.from_dict(entry))
                except TypeError:
                    continue
        return out

    def save(self, cameras: list[Camera]) -> None:
        """Persist atomically — see utils.atomic_write_text for why."""
        from .utils import atomic_write_text

        payload = {"cameras": [c.to_dict() for c in cameras]}
        atomic_write_text(
            self.path,
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        )

    def get(self, name: str) -> Camera | None:
        for c in self.load():
            if c.name == name:
                return c
        return None

    def create(self, camera: Camera) -> None:
        existing = self.load()
        validate_camera(camera, existing_names={c.name for c in existing})
        existing.append(camera)
        self.save(existing)

    def update(self, name: str, camera: Camera) -> None:
        existing = self.load()
        names = {c.name for c in existing}
        validate_camera(camera, existing_names=names, allow_existing=name)
        for i, c in enumerate(existing):
            if c.name == name:
                existing[i] = camera
                self.save(existing)
                return
        raise KeyError(name)

    def delete(self, name: str) -> bool:
        existing = self.load()
        new = [c for c in existing if c.name != name]
        if len(new) == len(existing):
            return False
        self.save(new)
        return True

    def names(self) -> list[str]:
        return [c.name for c in self.load()]
