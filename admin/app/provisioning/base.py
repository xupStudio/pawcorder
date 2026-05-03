"""Shared types for the Wi-Fi onboarding pipeline.

The pipeline has three stages — *discover*, *provision*, *confirm* —
each emitting events the orchestrator forwards over SSE to the admin
UI. Keeping the data classes here (rather than per-stage) lets the
orchestrator and the UI marshal one schema regardless of which transport
(BLE / SoftAP / QR / EspTouch) handled the actual cred push.
"""
from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from typing import ClassVar, Literal, Optional


# ---------------------------------------------------------------------------
# Vocabulary used across the pipeline
# ---------------------------------------------------------------------------

# Transport: how we get bytes onto the camera before it's on the LAN.
Transport = Literal[
    "ble",
    "softap",
    "qr",
    "esptouch",
    "wps",
    "matter",
    "homekit",
    "vendor_app",  # we can't push creds; user must use the vendor's app
]

# Capability: what we can actually do once we've identified a camera.
# - "auto":     push creds programmatically; user just needs to wait
# - "qr":       we render a QR for the camera to scan
# - "vendor":   detection only, hand off to the vendor's mobile app
# - "broadcast": EspTouch / WPS / SmartConfig — no point-to-point pairing,
#                we shout into the air and hope the camera is listening.
Capability = Literal["auto", "qr", "vendor", "broadcast"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredDevice:
    """One camera we noticed in pairing mode.

    ``transport`` and ``vendor`` come from fingerprints; ``mac`` is the
    BLE address (BD_ADDR) for BLE devices and the BSSID for SoftAP
    devices. Either form is enough for ``arrival_watcher`` to match
    against the LAN's ARP table once the camera connects.

    ``signal_dbm`` is RSSI when the transport reports it (BLE / Wi-Fi).
    The orchestrator uses it to sort the candidate list so the closest
    camera is first — handy when the user has more than one in pairing
    mode at the same time.
    """
    id: str  # stable client-side id (deduped by orchestrator)
    transport: Transport
    vendor: str
    model: str = ""
    label: str = ""        # human-readable name for the UI card
    mac: str = ""
    ssid: str = ""         # SoftAP SSID, when applicable
    signal_dbm: int = 0
    capability: Capability = "auto"
    # Carry through the matched fingerprint id so the orchestrator can
    # pick the right per-vendor provisioner without re-running the
    # fingerprint matcher.
    fingerprint_id: str = ""
    extra: dict = field(default_factory=dict)

    # Keys from `extra` that are safe + useful to expose to the admin
    # UI. Anything not in this list (raw BLE manufacturer-data bytes,
    # internal provisioner scratch, etc.) gets stripped by to_dict so
    # we don't leak protocol detail into the browser. Add to this list
    # only when a UI surface specifically needs the value.
    # ClassVar keeps dataclass from treating this as a per-instance field.
    _EXTRA_PUBLIC_KEYS: ClassVar[tuple[str, ...]] = (
        "vendor_app_ios",
        "vendor_app_android",
        "softap_ip",
    )

    def to_dict(self) -> dict:
        d = asdict(self)
        raw_extra = d.pop("extra", {}) or {}
        # Forward only the whitelisted keys (vendor-app deep links etc).
        # The rest stays server-side.
        d["extra"] = {
            k: raw_extra[k] for k in self._EXTRA_PUBLIC_KEYS if k in raw_extra
        }
        return d


@dataclass
class ProvisionerResult:
    """What every provisioner returns, regardless of transport.

    ``ok`` is the only field clients act on; ``message`` is the user-
    facing string the UI shows in the toast / status row. ``mac``
    propagates so ``arrival_watcher`` can pivot to the LAN-side wait
    without re-fingerprinting.
    """
    ok: bool
    message: str
    transport: Transport
    mac: str = ""
    # Some provisioners (QR, EspTouch) finish "successfully" but their
    # success means "we displayed/broadcast the creds" — the actual
    # join still has to happen on the camera. ``needs_arrival_watcher``
    # tells the orchestrator to keep watching for the MAC instead of
    # declaring victory immediately.
    needs_arrival_watcher: bool = True
    # Only set for QR provisioners. ``image_svg`` is the SVG markup the
    # admin UI renders inline; ``image_payload`` is the textual contents
    # of the QR for debugging.
    image_svg: str = ""
    image_payload: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProvisioningRequest:
    """Inputs to ``BaseProvisioner.provision``.

    Bundling the fields keeps per-vendor signatures uniform — adding a
    new provisioner doesn't ripple through the orchestrator.
    """
    device: DiscoveredDevice
    ssid: str
    psk: str
    auth: str = "wpa2-psk"


# ---------------------------------------------------------------------------
# Provisioner contract
# ---------------------------------------------------------------------------


class BaseProvisioner(abc.ABC):
    """Per-vendor / per-transport implementation hook.

    Lifecycle:
      1. ``BaseProvisioner.handles(device)`` — class method, queried by
         the orchestrator to route a discovered device to the right
         subclass. Cheap, no I/O.
      2. ``await provision(request)`` — the actual cred push. Must not
         raise on a normal "the camera said no" — return
         ``ProvisionerResult(ok=False, ...)``. Reserve exceptions for
         programmer error.
    """

    transport: Transport
    capability: Capability = "auto"

    @classmethod
    @abc.abstractmethod
    def handles(cls, device: DiscoveredDevice) -> bool:
        """True if this subclass should be picked for ``device``."""

    @abc.abstractmethod
    async def provision(self, request: ProvisioningRequest) -> ProvisionerResult:
        """Push Wi-Fi creds to ``request.device``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def vendor_app_handoff(
    device: DiscoveredDevice,
    *,
    vendor_app_url: str = "",
    note: str = "",
) -> ProvisionerResult:
    """Build a uniform "use the vendor app" result.

    Class-B vendors (Tapo / Wyze / Eufy / Ring / Nest / Tuya / Imou) all
    funnel through this helper so the admin UI gets one consistent
    handoff payload.
    """
    msg = (
        note
        or f"This {device.vendor} camera needs the official app to finish "
        f"Wi-Fi setup. After it joins the network, Pawcorder will detect it "
        f"automatically."
    )
    return ProvisionerResult(
        ok=True,
        message=msg,
        transport="vendor_app",
        mac=device.mac,
        needs_arrival_watcher=True,
        # The orchestrator surfaces vendor_app_url in the SSE event so
        # the UI can render a deep-link button. We pass it via extra
        # rather than a top-level field because most provisioners don't
        # populate it.
        image_payload=vendor_app_url,
    )


def normalise_mac(mac: str) -> str:
    """Lowercase, colon-separated MAC. Tolerant to ``-`` and ``.`` separators.

    Comparing MACs across BLE (``bleak`` returns ``aa:bb:...``), Wi-Fi
    (``nmcli`` returns ``AA:BB:...``), and ARP (``ip neigh`` returns
    ``aa:bb:...``) requires one canonical form. The arrival watcher
    matches case-insensitively but normalising here keeps the SSE event
    stream clean.
    """
    if not mac:
        return ""
    s = mac.replace("-", ":").replace(".", ":").lower().strip()
    parts = [p for p in s.split(":") if p]
    if len(parts) == 6 and all(len(p) == 2 for p in parts):
        return ":".join(parts)
    # Cisco-style 0123.4567.89ab — split into bytes.
    if len(s.replace(":", "")) == 12 and all(c in "0123456789abcdef" for c in s.replace(":", "")):
        flat = s.replace(":", "")
        return ":".join(flat[i : i + 2] for i in range(0, 12, 2))
    return s
