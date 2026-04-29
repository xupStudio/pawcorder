"""Per-brand RTSP path templates and compatibility metadata.

We test pawcorder against the brands listed below. New brands are added
when a user reports them working — we keep the list lean rather than
enumerating every model with a network port.

Each brand entry has:
  - `name`: human label for the UI
  - `rtsp_main`: example main-stream RTSP path (high-res, used for record)
  - `rtsp_sub`:  example sub-stream RTSP path (low-res, used for detect)
  - `default_user`: most common factory username
  - `default_rtsp_port`: standard RTSP port (almost always 554)
  - `two_way_audio`: True if the brand's typical models support talk-back
                     over RTSP backchannel (per Frigate docs / go2rtc tests)
  - `notes`: things the user needs to know before trying

Two-way audio support reflects the *brand's* typical capability. The
per-camera `two_way_audio` flag still defaults off — the user has to opt
in once they've tested it works on their specific model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class BrandSpec:
    name: str
    rtsp_main: str
    rtsp_sub: str
    default_user: str
    default_rtsp_port: int = 554
    two_way_audio: bool = False
    notes: str = ""
    # True for brands whose RTSP can't be enabled programmatically (Tapo's
    # encrypted-cloud protocol, Imou's cloud lock-in, Wyze's stock firmware
    # not speaking RTSP). The UI shows step-by-step in-app guidance for
    # these instead of running the auto-configure flow.
    manual_setup: bool = False


BRANDS: dict[str, BrandSpec] = {
    "reolink": BrandSpec(
        name="Reolink",
        rtsp_main="rtsp://USER:PASS@IP:554/h264Preview_01_main",
        rtsp_sub="rtsp://USER:PASS@IP:554/h264Preview_01_sub",
        default_user="admin",
        two_way_audio=True,
        notes="E-series and Duo-series have built-in 2-way audio. Set the camera "
              "encoding to H.264 (not H.265) in the Reolink app for best Frigate "
              "compatibility.",
    ),
    "tapo": BrandSpec(
        name="TP-Link Tapo",
        rtsp_main="rtsp://USER:PASS@IP:554/stream1",
        rtsp_sub="rtsp://USER:PASS@IP:554/stream2",
        default_user="admin",
        two_way_audio=True,
        manual_setup=True,
        notes="Create a separate camera account in the Tapo app first — your "
              "Tapo cloud login won't work for RTSP. C200/C210/C310 series "
              "tested OK.",
    ),
    "hikvision": BrandSpec(
        name="Hikvision",
        rtsp_main="rtsp://USER:PASS@IP:554/Streaming/Channels/101",
        rtsp_sub="rtsp://USER:PASS@IP:554/Streaming/Channels/102",
        default_user="admin",
        two_way_audio=False,
        notes="Talk-back via RTSP is unreliable across firmware versions. If "
              "you need it, an iSAPI integration is more stable but out of "
              "scope for pawcorder's MVP.",
    ),
    "dahua": BrandSpec(
        name="Dahua",
        rtsp_main="rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=0",
        rtsp_sub="rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=1",
        default_user="admin",
        two_way_audio=False,
    ),
    "amcrest": BrandSpec(
        name="Amcrest",
        rtsp_main="rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=0",
        rtsp_sub="rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=1",
        default_user="admin",
        two_way_audio=True,
        notes="Amcrest is a Dahua OEM — same RTSP layout. Most models support "
              "talk-back via the camera's backchannel.",
    ),
    "imou": BrandSpec(
        name="Imou",
        rtsp_main="rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=0",
        rtsp_sub="rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=1",
        default_user="admin",
        two_way_audio=False,
        manual_setup=True,
        notes="Imou is also a Dahua OEM. Enable RTSP in the Imou Life app first "
              "(Settings → Local config → RTSP).",
    ),
    "ubiquiti": BrandSpec(
        name="Ubiquiti UniFi Protect",
        rtsp_main="rtsp://USER:PASS@IP:7447/STREAM_KEY",
        rtsp_sub="rtsp://USER:PASS@IP:7447/STREAM_KEY_low",
        default_user="ubnt",
        default_rtsp_port=7447,
        two_way_audio=False,
        manual_setup=True,
        notes="UniFi Protect blocks RTSP by default — enable it per-camera in the "
              "Protect web UI (Cameras → camera → Settings → RTSP). The "
              "controller-driven 1-click flow (UDM/UNVR list → pick camera) is "
              "planned for a later release; for now treat UniFi as a manual brand "
              "and paste the RTSP URL the Protect UI shows you.",
    ),
    "axis": BrandSpec(
        name="Axis",
        rtsp_main="rtsp://USER:PASS@IP:554/axis-media/media.amp",
        rtsp_sub="rtsp://USER:PASS@IP:554/axis-media/media.amp?resolution=320x240",
        default_user="root",
        two_way_audio=False,
    ),
    "foscam": BrandSpec(
        name="Foscam",
        rtsp_main="rtsp://USER:PASS@IP:554/videoMain",
        rtsp_sub="rtsp://USER:PASS@IP:554/videoSub",
        default_user="admin",
        two_way_audio=False,
    ),
    "wyze": BrandSpec(
        name="Wyze (with RTSP firmware)",
        rtsp_main="rtsp://USER:PASS@IP:554/live",
        rtsp_sub="rtsp://USER:PASS@IP:554/live",
        default_user="admin",
        two_way_audio=False,
        manual_setup=True,
        notes="Stock Wyze firmware doesn't speak RTSP. You need the unofficial "
              "RTSP firmware on Wyze Cam v2/v3 — ask Wyze support, they ship it "
              "on request. Or run docker-wyze-bridge in front. We don't ship a "
              "wyze-bridge container by default.",
    ),
    "other": BrandSpec(
        name="Other (any RTSP camera)",
        rtsp_main="rtsp://USER:PASS@IP:554/<your-stream-path>",
        rtsp_sub="rtsp://USER:PASS@IP:554/<your-substream-path>",
        default_user="admin",
        two_way_audio=False,
        # NOT manual_setup: pawcorder probes ONVIF Profile S as the dispatcher
        # fallback for unknown brands. The cameras-page UI still renders the
        # supplementary "where to find the URL" guidance for this key — see
        # cameras.html, where the panel `x-show` triggers on
        # `manual_setup || key === 'other'`.
        manual_setup=False,
        notes="Any ONVIF / RTSP camera works if you can find the stream URLs. "
              "pawcorder will probe ONVIF first; falls back to your filled-in "
              "URLs if that doesn't work.",
    ),
}


def list_brands() -> list[dict]:
    """Snapshot of brand metadata for the UI."""
    return [
        {
            "key": key,
            "name": spec.name,
            "default_user": spec.default_user,
            "default_rtsp_port": spec.default_rtsp_port,
            "two_way_audio_supported": spec.two_way_audio,
            "rtsp_main": spec.rtsp_main,
            "rtsp_sub": spec.rtsp_sub,
            "notes": spec.notes,
            "manual_setup": spec.manual_setup,
        }
        for key, spec in BRANDS.items()
    ]


def get_brand(key: str) -> BrandSpec:
    return BRANDS.get(key, BRANDS["other"])


# --- RTSP URL builder -----------------------------------------------------
#
# Every vendor module used to ship its own ``rtsp_url(ip, user, password,
# ...)`` helper that just URL-encoded the credentials and pasted them into
# the brand's path template. Centralising the templating here means:
#
#   * the URL-encode discipline lives in one place,
#   * Hikvision-style channel arithmetic (``Streaming/Channels/101`` for
#     channel 1 main, ``201`` for channel 2 main) is handled uniformly,
#   * non-554 RTSP ports (UniFi 7447) are a kwarg, not a per-vendor branch,
#   * main.py's brand-aware fallback (test-camera button) can build a URL
#     for any brand without going through the vendor module.

_HIK_CHANNEL_RE = re.compile(r"/Channels/\d+$")


def build_rtsp_url(
    brand: str,
    ip: str,
    user: str,
    password: str,
    *,
    port: int | None = None,
    sub: bool = False,
    channel: int = 1,
) -> str:
    """Build the canonical RTSP URL for ``brand`` against ``ip``.

    ``user`` / ``password`` are URL-encoded so credentials with ``:`` or
    ``@`` don't corrupt the userinfo section. ``port`` defaults to the
    brand's ``default_rtsp_port`` (554 for most, 7447 for UniFi). For
    Hikvision-style templates ending in ``/Channels/<id>``, ``channel=N``
    rewrites the trailing id to ``N01`` / ``N02`` (main / sub); for every
    other template ``channel`` is ignored — the brand's path doesn't carry
    channel info in its URL grammar (Dahua puts it in the query string,
    Reolink hard-codes ``_01``, etc.).

    The brand-key fallback to ``other`` matches ``get_brand`` — building a
    URL for an unknown key returns the placeholder ``<your-stream-path>``
    template, which is more obviously broken in logs than a silent error.
    """
    spec = BRANDS.get(brand, BRANDS["other"])
    template = spec.rtsp_sub if sub else spec.rtsp_main
    eff_port = port if port is not None else spec.default_rtsp_port

    # Hikvision channel arithmetic: only when the template literally ends
    # in /Channels/<digits>. Anything else (Dahua's channel=1 query, Axis
    # axis-media, Reolink's _01_main) leaves the path verbatim.
    if _HIK_CHANNEL_RE.search(template):
        stream_id = channel * 100 + (2 if sub else 1)
        template = _HIK_CHANNEL_RE.sub(f"/Channels/{stream_id}", template)

    u = quote(user, safe="")
    p = quote(password, safe="")
    # Brand templates use the literal tokens USER / PASS / IP at the netloc.
    # Use a single-pass regex substitution rather than chained `.replace`
    # so that an encoded value containing one of the placeholder tokens
    # (e.g. a password that quotes to a string carrying "PASS") cannot
    # corrupt a subsequent step. \b word boundaries also keep us from
    # touching the literals if they're embedded inside a path segment.
    substitutions = {"USER": u, "PASS": p, "IP": ip}
    url = re.sub(
        r"\b(USER|PASS|IP)\b",
        lambda m: substitutions[m.group(1)],
        template,
    )
    if eff_port != spec.default_rtsp_port:
        # Replace the first ``:<default_port>`` after ``@ip`` with the
        # caller's port. We anchor on ``@<ip>:<default_port>`` to avoid
        # mangling a port that happens to appear elsewhere in the path
        # (e.g. ``?port=554`` in a hypothetical query string).
        url = url.replace(
            f"@{ip}:{spec.default_rtsp_port}",
            f"@{ip}:{eff_port}",
            1,
        )
    return url
