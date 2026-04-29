"""Brand-aware dispatcher for camera auto-configuration.

The cameras admin page lets the user pick a brand from `camera_compat.BRANDS`.
For each automatable brand we have a small per-vendor module with the same
public shape:

    async def auto_configure(ip, user, password) -> dict
        # returns {"device", "link", "connection_type", "rtsp_main", "rtsp_sub"}

This module routes the brand string to the right module so the rest of the
admin (main.py, _run_camera_test) doesn't have to know about per-vendor
quirks. Brands flagged `manual_setup=True` in camera_compat (Tapo cloud
lock-in, Imou cloud lock-in, Wyze stock-firmware) skip the dispatch and
return a `{"manual": True}` sentinel — the cameras-page UI surfaces
step-by-step in-app instructions for those.

Unknown brand keys (or the "other" catch-all) fall back to ONVIF Profile S
discovery via `onvif_helper`.

UniFi Protect is intentionally NOT in the per-camera dispatch table:
its `auto_configure` takes a controller URL and returns a list of
cameras. Wiring that into the single-camera onboarding flow requires
a different UX (pick-from-list after controller login), tracked
separately.
"""
from __future__ import annotations

import logging
from types import ModuleType

from . import (
    axis_api,
    camera_api,        # Reolink
    camera_compat,
    dahua_api,
    foscam_api,
    hikvision_api,
    onvif_helper,
)

logger = logging.getLogger("pawcorder.camera_setup")


# Per-brand → module dispatch. We store the module reference (not the
# function) so attribute lookup happens on every call: tests monkeypatch
# `<module>.auto_configure` and the dispatcher picks up the stub on the
# next invocation regardless of import ordering.
#
# Keys must match camera_compat.BRANDS keys. Every module here exposes
# `async def auto_configure(ip, user, password) -> dict` with the same
# return shape as camera_api.auto_configure plus rtsp_main / rtsp_sub.
_BRAND_MODULES: dict[str, ModuleType] = {
    "reolink":   camera_api,
    "hikvision": hikvision_api,
    "dahua":     dahua_api,
    "amcrest":   dahua_api,    # Dahua OEM — same CGI surface
    "axis":      axis_api,
    "foscam":    foscam_api,
}


def _manual_response() -> dict:
    """Sentinel returned for brands the user has to set up by hand."""
    return {
        "device": None,
        "link": None,
        "connection_type": "unknown",
        "rtsp_main": "",
        "rtsp_sub": "",
        "manual": True,
    }


def is_manual_brand(brand: str) -> bool:
    """True if pawcorder can't programmatically enable RTSP for this brand.

    Source of truth is camera_compat.BRANDS[brand].manual_setup. Unknown
    brand keys are NOT manual — they fall back to ONVIF discovery.
    """
    spec = camera_compat.BRANDS.get(brand)
    return spec is not None and spec.manual_setup


async def auto_configure_for_brand(brand: str, ip: str, user: str, password: str) -> dict:
    """Talk to the camera via the brand's API to read device info and
    enable RTSP if needed.

    Routing:
      - manual_setup brands (tapo / imou / wyze / other) → manual sentinel
      - known automatable brand → that module's auto_configure
      - unknown brand → ONVIF Profile S fallback

    Raises PermissionError on bad credentials and RuntimeError on network
    failures (forwarded from the per-vendor module).
    """
    if is_manual_brand(brand):
        return _manual_response()
    module = _BRAND_MODULES.get(brand)
    if module is not None:
        # Look up auto_configure on the module each call so test fixtures
        # that patch `<vendor>.auto_configure` are respected.
        return await module.auto_configure(ip, user, password)
    logger.info("Unknown brand %r — falling back to ONVIF Profile S", brand)
    return await onvif_helper.auto_configure(ip, user, password)
