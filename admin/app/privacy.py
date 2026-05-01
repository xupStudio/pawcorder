"""Privacy mode — pause recording when the owner is home.

Two ways to know you're home:

  - **Tailscale presence** (preferred): when one of the user's tagged
    Tailscale devices (their phone, their laptop) is online and on the
    home network, we treat that as "person home".
  - **Manual toggle**: a button in the UI that flips Frigate's record
    mode on/off via go2rtc / docker.

When privacy mode is active we re-render the Frigate config with
`record.enabled: false` for every camera and restart Frigate. This
is honest about what we're doing — recordings really are off, not just
hidden — which matches the user's expectation when they hit "privacy".

Wiring (avoid breaking by re-running `git grep`):

  - `PrivacyMonitor` (this file) polls Tailscale every 60s and, when
    `paused_now` flips, re-renders the Frigate config and restarts the
    Frigate container.
  - `config_store.render_frigate_config` reads `is_paused()` and feeds
    `recording_paused` into the Jinja2 template.
  - `frigate.template.yml`'s `record.enabled` block looks at that flag.
  - `main.lifespan` starts/stops the monitor.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("pawcorder.privacy")

# State file lives alongside .env; a single line per knob keeps it shell-friendly.
DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
STATE_PATH = DATA_DIR / "config" / "privacy.json"


@dataclass
class PrivacyState:
    enabled: bool = False                 # the master toggle
    auto_pause_when_home: bool = False    # use Tailscale presence
    paused_now: bool = False              # the resolved live state
    # Reason for the UI: a translation key + optional argument so the
    # message renders in the user's language. Keys are resolved against
    # i18n.T (PRIVACY_REASON_*).
    reason_key: str = ""
    reason_arg: str = ""
    home_devices: list[str] = None        # Tailscale device names

    def __post_init__(self):
        if self.home_devices is None:
            self.home_devices = []

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "auto_pause_when_home": self.auto_pause_when_home,
            "paused_now": self.paused_now,
            "reason_key": self.reason_key,
            "reason_arg": self.reason_arg,
            "home_devices": list(self.home_devices),
        }


# ---- persistence -------------------------------------------------------

def load_state() -> PrivacyState:
    if not STATE_PATH.exists():
        return PrivacyState()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PrivacyState()
    return PrivacyState(
        enabled=bool(data.get("enabled", False)),
        auto_pause_when_home=bool(data.get("auto_pause_when_home", False)),
        paused_now=bool(data.get("paused_now", False)),
        reason_key=str(data.get("reason_key", "")),
        reason_arg=str(data.get("reason_arg", "")),
        home_devices=list(data.get("home_devices") or []),
    )


def save_state(state: PrivacyState) -> None:
    """Persist privacy state atomically — see utils.atomic_write_text."""
    from .utils import atomic_write_text

    atomic_write_text(STATE_PATH, json.dumps(state.to_dict(), indent=2))


# ---- presence detection ------------------------------------------------

async def tailscale_devices_online() -> list[str]:
    """List Tailscale device hostnames that are currently online.

    We try, in order:
      1. The Tailscale local API (`http://100.100.100.100/`-equivalent
         via the `tailscale status --json` command if available)
      2. A fallback: `tailscale status --json` via subprocess

    Returns empty list if Tailscale isn't installed / running. Never
    raises — privacy detection should fail-open (i.e. NOT pause).
    """
    # The clean path is shelling out to `tailscale status --json`.
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=4)
        if proc.returncode != 0:
            return []
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return []

    try:
        data = json.loads(stdout.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return []

    online: list[str] = []
    peers = (data.get("Peer") or {}).values()
    for peer in peers:
        if peer.get("Online"):
            host = (peer.get("HostName") or peer.get("DNSName") or "").split(".", 1)[0]
            if host:
                online.append(host)
    # The host itself.
    self_node = data.get("Self") or {}
    if self_node.get("Online"):
        host = (self_node.get("HostName") or "").split(".", 1)[0]
        if host:
            online.append(host)
    return online


async def evaluate_async(state: Optional[PrivacyState] = None) -> PrivacyState:
    """Return a fresh PrivacyState reflecting current presence.

    Saves nothing — caller chooses whether to persist. The Frigate config
    re-renderer uses `paused_now` to decide whether to emit a global
    `record.enabled: false`.
    """
    s = state or load_state()
    if not s.enabled:
        s.paused_now = False
        s.reason_key = "PRIVACY_REASON_OFF"
        s.reason_arg = ""
        return s

    # Manual mode: paused_now is whatever the user set. We don't override.
    if not s.auto_pause_when_home:
        s.reason_key = "PRIVACY_REASON_MANUAL"
        s.reason_arg = ""
        return s

    online = await tailscale_devices_online()
    matches = [d for d in online if d in s.home_devices]
    if matches:
        s.paused_now = True
        s.reason_key = "PRIVACY_REASON_HOME_ONLINE"
        s.reason_arg = ", ".join(matches)
    else:
        s.paused_now = False
        s.reason_key = "PRIVACY_REASON_NOBODY_HOME"
        s.reason_arg = ""
    return s


def is_paused(cfg_data_dir: Optional[Path] = None) -> bool:
    """Synchronous, fast accessor used at config-render time.

    Returns the last-saved `paused_now` flag. The async evaluator updates
    this when it runs. We don't try to detect Tailscale at render time —
    that would block on subprocess spawn.
    """
    s = load_state()
    return s.enabled and s.paused_now


# ---- background monitor -----------------------------------------------

PRIVACY_POLL_INTERVAL_SECONDS = 60


class PrivacyMonitor:
    """Polls Tailscale presence on a schedule and reactively re-renders
    the Frigate config + restarts the Frigate container when the
    `paused_now` flag flips.

    Why a monitor and not just on-demand evaluation: privacy mode has to
    work *while the user is asleep / away from the admin page*. The
    `/api/privacy/evaluate` route is only hit when the user is staring
    at the privacy page, which is most of the time exactly when they
    are NOT home — i.e. when they want recording to keep going.

    Contract: this monitor MUST NOT crash on any tick. We swallow every
    exception so a Frigate-restart hiccup doesn't take down the whole
    privacy loop.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Last value of `paused_now` we acted on. None = haven't observed yet.
        self._last_acted: Optional[bool] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._last_acted = None
            self._task = asyncio.create_task(self._run(), name="privacy-monitor")
            logger.info("privacy monitor started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("privacy monitor stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("privacy monitor tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=PRIVACY_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        state = await evaluate_async()
        save_state(state)
        effective_pause = state.enabled and state.paused_now

        # First tick after start(): we cannot trust that the rendered
        # config.yml on disk reflects the current privacy state. The
        # admin container could have been killed mid-transition; the
        # user could have edited cameras.yml outside the UI; Frigate
        # could have been started from a stale config.yml after a host
        # reboot. Reconcile unconditionally — if there are no cameras
        # yet, _reapply is a no-op anyway.
        if self._last_acted is None:
            try:
                await self._reapply()
                self._last_acted = effective_pause
            except Exception:  # noqa: BLE001
                logger.warning("privacy reapply failed at boot; will retry next tick", exc_info=True)
                # Leave _last_acted=None so we keep retrying every tick
                # rather than getting stuck.
            return

        if self._last_acted == effective_pause:
            return  # steady state — nothing to do

        # Transition. Only commit _last_acted on success so a docker
        # hiccup doesn't permanently desync us from Frigate's real state.
        try:
            await self._reapply()
            self._last_acted = effective_pause
        except Exception:  # noqa: BLE001
            logger.warning("privacy reapply failed; will retry next tick", exc_info=True)

    async def _reapply(self) -> None:
        """Re-render Frigate config + restart the container.

        Two failure modes are *expected* and silenced:
          - No cameras yet (initial setup) — return without doing anything.
          - Frigate container doesn't exist yet — RuntimeError from
            docker_ops.restart_frigate; treated as "nothing to restart".

        Any other exception (Docker daemon unreachable, transient network
        glitch, etc.) propagates so _tick() can decide whether to retry.
        Local imports break a circular import (config_store would
        otherwise import privacy at module load time).
        """
        from . import cameras_store, config_store, docker_ops

        cfg = config_store.load_config()
        cams = cameras_store.CameraStore().load()
        if not cams:
            return  # nothing to render against; setup not complete yet
        config_store.write_frigate_config(cfg, cams)
        try:
            docker_ops.restart_frigate()
        except RuntimeError as exc:
            # "Frigate container does not exist yet" — fine, the next
            # `make up` will pick up the new config.yml on its own.
            logger.debug("privacy reapply: skipping restart (%s)", exc)
            return
        logger.info("privacy monitor re-applied; paused=%s",
                    self._last_acted if self._last_acted is not None else "(boot)")


monitor = PrivacyMonitor()
