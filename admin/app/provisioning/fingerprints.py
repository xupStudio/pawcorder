"""Vendor identification database.

This is the single source of truth for "which camera is in front of us"
across every transport (BLE / SoftAP / QR-receive). Each fingerprint
carries:

  - id:                   stable string the orchestrator passes around
  - vendor:               camera_compat brand key
                          (matches camera_compat.BRANDS so the
                          post-arrival auto-configure step has the
                          right routing)
  - transport_capability: which transports we can drive (used to gate
                          which subset of provisioners gets registered)
  - ble_service_uuids:    BLE service UUIDs the device advertises in
                          pairing mode
  - ble_manufacturer_id:  Bluetooth SIG company id (16-bit) if the
                          device leans on manufacturer-data instead of
                          UUIDs
  - softap_ssid_patterns: regex-like substrings the SoftAP SSID matches
                          (case-insensitive)
  - mac_oui_prefixes:     IEEE OUI prefixes (first 6 hex of the MAC,
                          colon-free) used as a tiebreaker / for the
                          arrival watcher

Fingerprints with ``capability == "vendor"`` are detection-only —
proprietary protocols where no permissive-license OSS code completes
the cred push. We still detect them so the UI can offer the matching
vendor-app deep link and the arrival watcher knows what to wait for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import Capability, Transport


@dataclass(frozen=True)
class Fingerprint:
    id: str
    vendor: str            # camera_compat key
    label: str             # human-readable, fed into the UI card
    transports: tuple[Transport, ...]
    capability: Capability
    ble_service_uuids: tuple[str, ...] = ()
    ble_manufacturer_id: int | None = None
    ble_local_name_patterns: tuple[str, ...] = ()
    softap_ssid_patterns: tuple[str, ...] = ()
    mac_oui_prefixes: tuple[str, ...] = ()
    # Free-form blob the per-vendor provisioner reads. Examples:
    # ``softap_endpoint``, ``vendor_app_url``, ``qr_payload_template``.
    metadata: dict = field(default_factory=dict)


# UUIDs are lower-case 36-char canonical form. ``bleak`` normalises
# what it reports to that form, so direct string compare is enough.

# Bluetooth SIG-assigned 16-bit UUIDs are widened to 128-bit using the
# base ``0000xxxx-0000-1000-8000-00805f9b34fb`` form when matched.
def _short(uuid16: int) -> str:
    return f"0000{uuid16:04x}-0000-1000-8000-00805f9b34fb"


# ---------------------------------------------------------------------------
# Class A — protocols we can drive end-to-end
# ---------------------------------------------------------------------------

FINGERPRINTS: tuple[Fingerprint, ...] = (
    # Matter — assigned 16-bit service UUID 0xFFF6 by the CSA. Any
    # Matter-certified camera in commissioning mode advertises it.
    Fingerprint(
        id="matter-generic",
        vendor="other",
        label="Matter-compatible camera",
        transports=("matter", "ble"),
        capability="auto",
        ble_service_uuids=(_short(0xFFF6),),
        metadata={
            "doc_url": "https://csa-iot.org/all-solutions/matter/",
            "note": "any Matter-certified camera works; device-specific brand "
                    "is read from the operational dataset after commissioning",
        },
    ),
    # HomeKit Accessory Protocol — service UUID 0000FE5C-... is the
    # SIG-assigned advertisement service for HomeKit accessories in
    # pairing mode (per the HAP-BLE spec).
    Fingerprint(
        id="homekit-generic",
        vendor="other",
        label="HomeKit-compatible camera",
        transports=("homekit", "ble"),
        capability="auto",
        ble_service_uuids=(_short(0xFE5C),),
        metadata={
            "doc_url": "https://developer.apple.com/homekit/specification/",
            "note": "Aqara / Eve / Logitech HomeKit cams hit this branch",
        },
    ),

    # Foscam SoftAP — historic SSID pattern documented in libpyfoscam
    # plus Foscam's setup-via-AP guide. Models since ~2015.
    Fingerprint(
        id="foscam-softap",
        vendor="foscam",
        label="Foscam (SoftAP setup mode)",
        transports=("softap",),
        capability="auto",
        softap_ssid_patterns=(r"^IPC-[A-Z0-9]{6,}", r"^foscam[-_]"),
        metadata={
            "softap_ip": "192.168.0.1",
            "cgi_path": "/cgi-bin/CGIProxy.fcgi",
        },
    ),
    # Espressif ESP32 reference SoftAP — used by countless white-label
    # cams shipping the unmodified ESP-IDF wifi_provisioning component.
    # SSID prefix ``PROV_`` is the IDF default; some integrators rename.
    Fingerprint(
        id="espressif-softap",
        vendor="other",
        label="ESP32-based camera (SoftAP)",
        transports=("softap",),
        capability="auto",
        softap_ssid_patterns=(r"^PROV_", r"^ESP[_-]?[A-F0-9]{6}"),
        metadata={
            "softap_ip": "192.168.4.1",
            "endpoint": "/prov-config",
        },
    ),
    # Older D-Link / TP-Link cams expose HNAP1 over the SoftAP. Same
    # SOAP envelope; SSID prefix differs.
    Fingerprint(
        id="dlink-hnap-softap",
        vendor="other",
        label="D-Link / TP-Link legacy (SoftAP)",
        transports=("softap",),
        capability="auto",
        softap_ssid_patterns=(r"^dlink[-_]", r"^TP-LINK_[A-F0-9]{6}_"),
        metadata={
            "softap_ip": "192.168.0.1",
            "hnap_path": "/HNAP1/",
        },
    ),
    # Dahua / Amcrest SoftAP — newer firmwares expose configManager
    # over HTTP-Digest after the user joins ``DAHUA_xxxxxx``.
    Fingerprint(
        id="dahua-softap",
        vendor="dahua",
        label="Dahua / Amcrest (SoftAP)",
        transports=("softap",),
        capability="auto",
        # Dahua SSIDs commonly start with one of: ``DAHUA_xxxx``,
        # ``DH-IPC-…`` (model-prefix), ``Amcrest-xxxx`` (the OEM brand
        # ships its own prefix). The third pattern catches all of the
        # ``DH-`` / ``DH_`` model-letter forms.
        softap_ssid_patterns=(r"^dahua[-_]", r"^Amcrest[-_]", r"^DH[-_]\w+"),
        metadata={
            "softap_ip": "192.168.1.108",
            "config_path": "/cgi-bin/configManager.cgi",
        },
    ),

    # No-name / OEM cameras shipping the popular Chinese app stacks
    # (iCSee, CamHi, V380 Pro, MIPC, EseeCloud, YCC365, VStarcam Eye4).
    # These all broadcast distinctive SoftAP SSIDs but each app vendor
    # uses its own prefix. Without these fingerprints the dropshipped
    # cameras most pet owners actually buy on Shopee / Lazada / Amazon
    # don't even appear in the wireless onboarding list.
    #
    # capability="vendor" because their cred-push protocols are vendor-
    # proprietary (not OSS-replicable). We surface the SoftAP so the
    # user gets a clear "use your camera's original app, then come back
    # — Pawcorder will detect when it joins" instead of a dead end.
    Fingerprint(
        id="no-name-camera-softap",
        vendor="other",
        label="No-name camera (SoftAP setup mode)",
        transports=("softap", "vendor_app"),
        capability="vendor",
        softap_ssid_patterns=(
            r"^IPC365[-_]",        # YCC365 Plus app
            r"^iCSee[-_]",         # iCSee app — JOOAN, ANRAN, Sannce …
            r"^MV[+\-_]",          # MipCam / MIPC family
            r"^MIPC[-_]",
            r"^V380[-_]",          # V380 Pro
            r"^JXLCAM[-_]",        # JXLCAM-W
            r"^ICAM[-_]",          # CamHi / CamHipro
            r"^ATOM[-_]",          # EseeCloud
            r"^EYE4[-_]",          # VStarcam Eye4
            r"^EeIPC",             # E-cam
            r"^IP[-_]?CAMERA[-_]",
            r"^Wireless[-_]Camera[-_]",
            r"^WIFI[-_]?CAM[-_]",
            r"^IPCAM[-_]",
            r"^SmartCam[-_]",
            r"^GoodCam[-_]",
            r"^HD_AP_",
        ),
        metadata={
            # Most no-name cams expose a captive-portal-style HTTP setup
            # page on one of these IPs once joined. We surface this so
            # the UI can offer a "open camera setup page" link.
            "softap_ip_candidates": ("192.168.4.1", "192.168.10.1", "192.168.1.1"),
        },
    ),

    # ---- Class B: detection-only ------------------------------------

    # Tapo BLE — service UUID 0000FFF0-... is observed across the C200/
    # C210/C310 family per the public RE writeups. We can't push creds
    # without the proprietary handshake, but we surface the deep-link.
    Fingerprint(
        id="tapo-ble",
        vendor="tapo",
        label="TP-Link Tapo (vendor app required)",
        transports=("ble", "vendor_app"),
        capability="vendor",
        ble_service_uuids=(_short(0xFFF0),),
        ble_local_name_patterns=(r"^Tapo_", r"^TP-LINK_"),
        mac_oui_prefixes=("9c5322", "60a4b7"),  # known TP-Link blocks
        metadata={
            "vendor_app_ios": "https://apps.apple.com/app/id1472718009",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.tplink.iot",
        },
    ),
    # Wyze — uses Bluetooth SIG company id 0x010E (Logitech-allocated
    # BLE block historically reused by Wyze for v3) plus a
    # device-name prefix.
    Fingerprint(
        id="wyze-ble",
        vendor="wyze",
        label="Wyze Cam (vendor app required)",
        transports=("ble", "vendor_app"),
        capability="vendor",
        ble_local_name_patterns=(r"^WYZECAM", r"^Wyze[ _]"),
        mac_oui_prefixes=("2cab33", "7c787e"),
        metadata={
            "vendor_app_ios": "https://apps.apple.com/app/id1288415553",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.hualai",
        },
    ),
    # Eufy / Anker — detected via local name + Anker OUI.
    Fingerprint(
        id="eufy-ble",
        vendor="other",
        label="Eufy / Anker camera (vendor app required)",
        transports=("ble", "vendor_app"),
        capability="vendor",
        ble_local_name_patterns=(r"^eufy", r"^T8\d+",),  # T8210, T8400 …
        mac_oui_prefixes=("a4c138", "8c853d"),
        metadata={
            "vendor_app_ios": "https://apps.apple.com/app/id1424956516",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.oceanwing.battery.cam",
        },
    ),
    # Reolink Argus / battery cams advertise BLE for setup; the wire
    # format is undocumented per Reolink's own KB. We detect, defer.
    Fingerprint(
        id="reolink-ble",
        vendor="reolink",
        label="Reolink (battery / Argus, vendor app required)",
        transports=("ble", "vendor_app"),
        capability="vendor",
        ble_local_name_patterns=(r"^Reolink", r"^Argus"),
        mac_oui_prefixes=("ec71db", "b4a9fc"),
        # Verified 2026-05-03 via apps.apple.com / play.google.com.
        metadata={
            "vendor_app_ios": "https://apps.apple.com/app/id995927563",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.mcu.reolink",
        },
    ),
    # Ring (Amazon) — heavily cloud-locked. Detection only.
    Fingerprint(
        id="ring-ble",
        vendor="other",
        label="Ring (Amazon, vendor app required)",
        transports=("ble", "vendor_app"),
        capability="vendor",
        ble_local_name_patterns=(r"^Ring[_ -]",),
        # Verified 2026-05-03. Apple lists this as "Ring - Always Home".
        metadata={
            "vendor_app_ios": "https://apps.apple.com/app/id926252661",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.ringapp",
        },
    ),
    # Nest / Google — same situation. Modern Nest cams advertise as
    # ``Google Nest`` over BLE but onboarding is locked to Google Home.
    Fingerprint(
        id="nest-ble",
        vendor="other",
        label="Google Nest (vendor app required)",
        transports=("ble", "vendor_app"),
        capability="vendor",
        ble_local_name_patterns=(r"^Google Nest", r"^GNAxx",),
        # Verified 2026-05-03. Modern Nest cams onboard through the
        # Google Home app rather than the legacy Nest one.
        metadata={
            "vendor_app_ios": "https://apps.apple.com/app/id680819774",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.google.android.apps.chromecast.app",
        },
    ),
    # Tuya BLE white-label — UUID 0xFD50 + manufacturer id 0x07D0 is
    # the SmartLife signature. Cred push needs Tuya cloud auth; OSS
    # cannot complete the join.
    Fingerprint(
        id="tuya-ble",
        vendor="other",
        label="Tuya / Smart Life camera (vendor app required)",
        transports=("ble", "vendor_app"),
        capability="vendor",
        ble_service_uuids=(_short(0xFD50),),
        ble_manufacturer_id=0x07D0,
        metadata={
            # Verified 2026-05-03. "Smart Life - Smart Living" by
            # Volcano Technology is the consumer-facing Tuya app; the
            # alternate "Tuya Smart" app uses a different ID and
            # account namespace, so we standardise on Smart Life here.
            "vendor_app_ios": "https://apps.apple.com/app/id1115101477",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.tuya.smartlife",
        },
    ),
    # Tuya / Smart Life SoftAP — when the BLE-pairing path fails (older
    # firmware, BLE radio borked, or "EZ mode" timeout) Tuya cameras
    # fall back to broadcasting a ``SmartLife-XXXX`` or ``SmartCam_xxx``
    # AP. Cred push is still cloud-tied so we surface for vendor-app
    # handoff. The 4-digit zero suffix (``SmartLife-0000``) is the
    # observed white-label default before the camera has been claimed.
    Fingerprint(
        id="tuya-softap",
        vendor="other",
        label="Tuya / Smart Life camera (SoftAP mode)",
        transports=("softap", "vendor_app"),
        capability="vendor",
        softap_ssid_patterns=(
            r"^SmartLife[-_]",
            r"^SL[-_][A-Z0-9]{4,}",         # newer firmwares shorten to "SL-..."
            r"^Tuya[-_]",
            r"^Tuya_AP[-_]?",
        ),
        metadata={
            "softap_ip_candidates": ("192.168.176.1", "192.168.4.1"),
            # Verified 2026-05-03. "Smart Life - Smart Living" by
            # Volcano Technology is the consumer-facing Tuya app; the
            # alternate "Tuya Smart" app uses a different ID and
            # account namespace, so we standardise on Smart Life here.
            "vendor_app_ios": "https://apps.apple.com/app/id1115101477",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.tuya.smartlife",
        },
    ),
    # Imou — Dahua-cloud variant. SoftAP exists but the QR payload is
    # opaque; we detect and defer.
    Fingerprint(
        id="imou-softap",
        vendor="imou",
        label="Imou (vendor app required)",
        transports=("softap", "vendor_app"),
        capability="vendor",
        softap_ssid_patterns=(r"^Imou_", r"^LECHANGE_"),
        # Verified 2026-05-03. Old "smartlifeforplus" / "id1474183766"
        # both 404 — current package is "smartlifeiot" (renamed
        # post-rebrand from Lechange).
        metadata={
            "vendor_app_ios": "https://apps.apple.com/app/id1071165451",
            "vendor_app_android": "https://play.google.com/store/apps/details?id=com.mm.android.smartlifeiot",
        },
    ),
    # Reolink wired-only setup with QR — feed the camera a Reolink-style
    # XML QR. The capability is ``qr`` so the orchestrator routes the
    # request to qr_reolink instead of a SoftAP / BLE provisioner.
    Fingerprint(
        id="reolink-qr",
        vendor="reolink",
        label="Reolink (QR setup)",
        transports=("qr",),
        capability="qr",
        # Triggered by user choice, not advertisement — no UUIDs.
        metadata={
            "qr_format": "reolink-xml",
        },
    ),
    Fingerprint(
        id="generic-qr",
        vendor="other",
        label="Camera that scans Wi-Fi QR codes",
        transports=("qr",),
        capability="qr",
        metadata={
            "qr_format": "wifi-scheme",
            "note": "Hikvision Hik-Connect setup, some Aqara cams, Pawcorder "
                    "test fixtures",
        },
    ),
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def by_id(fingerprint_id: str) -> Fingerprint | None:
    for f in FINGERPRINTS:
        if f.id == fingerprint_id:
            return f
    return None


def for_transport(transport: Transport) -> list[Fingerprint]:
    return [f for f in FINGERPRINTS if transport in f.transports]


# ---------------------------------------------------------------------------
# Matchers
# ---------------------------------------------------------------------------


def match_ble(
    *,
    advertised_uuids: list[str],
    local_name: str,
    mac: str,
    manufacturer_ids: list[int],
) -> Fingerprint | None:
    """Best-match fingerprint for a BLE advertisement.

    Match order is deterministic so the same input always yields the
    same output. We prefer:
      1. Service UUID hits — the strongest signal a camera intentionally
         advertises which protocol it speaks.
      2. Manufacturer-id hits — Tuya, Wyze etc. use this when they don't
         allocate a service UUID.
      3. Local-name regex — last resort because SSIDs / device names get
         renamed by users / firmwares.
    """
    advertised = {u.lower() for u in advertised_uuids}
    name = (local_name or "").strip()
    oui = mac.replace(":", "").lower()[:6]

    # 1) UUID
    for f in FINGERPRINTS:
        if not f.ble_service_uuids:
            continue
        if any(u.lower() in advertised for u in f.ble_service_uuids):
            return f

    # 2) Manufacturer id
    for f in FINGERPRINTS:
        if f.ble_manufacturer_id is not None and f.ble_manufacturer_id in manufacturer_ids:
            return f

    # 3) Local name
    if name:
        for f in FINGERPRINTS:
            for pat in f.ble_local_name_patterns:
                if re.match(pat, name, flags=re.IGNORECASE):
                    return f

    # 4) MAC OUI as final tiebreaker — only if the brand is one we'd
    # otherwise miss. Avoids matching innocuous Bluetooth peripherals
    # that happen to share an OUI with a camera vendor.
    if oui:
        for f in FINGERPRINTS:
            if oui in f.mac_oui_prefixes and f.ble_local_name_patterns:
                return f

    return None


def match_softap(ssid: str) -> Fingerprint | None:
    """Match a SoftAP SSID against the fingerprints with SSID patterns."""
    if not ssid:
        return None
    for f in FINGERPRINTS:
        for pat in f.softap_ssid_patterns:
            if re.search(pat, ssid, flags=re.IGNORECASE):
                return f
    return None
