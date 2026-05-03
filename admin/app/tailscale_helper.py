"""Tailscale auto-detect + install helper.

The /mobile page currently asks the user to:
  1. Run ``./scripts/install-tailscale.sh`` on the Pawcorder host
  2. Run ``sudo tailscale up`` and sign in via a URL
  3. Manually paste their tailnet hostname into Pawcorder

This module wraps those steps so the admin UI can offer:
  - A status probe (``status()``) that returns the current state and the
    tailnet hostname when Tailscale is already running, eliminating the
    paste step entirely on already-configured hosts.
  - An install endpoint (``install()``) that subprocess-runs the existing
    install script and tails its output for the UI.
  - An "up" endpoint (``up()``) that captures the auth URL printed by
    ``tailscale up`` so the admin can render it as a clickable link.

We deliberately don't shell out to a "tailscale login + interactive auth"
path — the user's browser is the only place that can complete that flow.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pawcorder.tailscale")

# `tailscale up` prints "To authenticate, visit: https://login.tailscale.com/a/..."
# on stderr. The exact wording varies by version; capture any URL on the
# tailscale.com auth domain.
_AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9]+")


@dataclass
class Status:
    installed: bool
    running: bool
    logged_in: bool
    hostname: str = ""    # FQDN like "mybox.tail-abcd.ts.net"
    tailnet: str = ""     # short-form like "tail-abcd"
    error: str = ""       # populated when the CLI is present but errored


def _run(args: list[str], *, timeout: float = 5.0) -> tuple[int, str, str]:
    """Subprocess wrapper. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return 127, "", "tailscale binary not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def status() -> Status:
    """Snapshot the local Tailscale state. Never raises."""
    if shutil.which("tailscale") is None:
        return Status(installed=False, running=False, logged_in=False)

    rc, out, err = _run(["tailscale", "status", "--json"])
    if rc != 0:
        # Common case: daemon not running yet → exit 1 + "Tailscale is stopped".
        return Status(
            installed=True, running=False, logged_in=False,
            error=(err or out).strip()[:200],
        )

    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return Status(installed=True, running=False, logged_in=False, error=f"parse: {exc}")

    self_node = data.get("Self") or {}
    backend_state = data.get("BackendState", "")  # "Running" / "NeedsLogin" / etc.

    # `MagicDNSSuffix` looks like "tail-abcd.ts.net"; we strip the trailing
    # ".ts.net" to get the short tailnet name.
    magic = (data.get("MagicDNSSuffix") or "").strip(".")
    tailnet = magic.removesuffix(".ts.net") if magic.endswith(".ts.net") else magic

    return Status(
        installed=True,
        running=backend_state == "Running",
        logged_in=bool(self_node.get("ID")),
        hostname=(self_node.get("DNSName") or "").rstrip("."),
        tailnet=tailnet,
    )


def install_script_path() -> Path:
    """Repo-relative path of the install script. Resolved at call time so
    tests can monkeypatch the project root."""
    # admin/app/__file__ → admin/app/ → ../../ = project root
    here = Path(__file__).resolve().parent.parent.parent
    return here / "scripts" / "install-tailscale.sh"


def install() -> tuple[bool, str]:
    """Run scripts/install-tailscale.sh. Returns (ok, combined_output).

    Output is truncated to 4 KB — the script normally prints ~30 lines, so
    runaway output means something is wrong and the user should look at
    container logs anyway.
    """
    script = install_script_path()
    if not script.exists():
        return False, f"install script not found at {script}"
    rc, out, err = _run(["bash", str(script)], timeout=120.0)
    combined = (out + err).strip()[:4096]
    return rc == 0, combined


def up_capture_auth_url() -> tuple[bool, str, str]:
    """Run ``tailscale up`` and capture the auth URL.

    Returns (ok, auth_url, raw_output). On a host that's already logged in,
    ``auth_url`` is empty and ``ok`` is True — the caller should re-probe
    ``status()`` to verify.
    """
    rc, out, err = _run(["tailscale", "up", "--reset"], timeout=15.0)
    combined = (out + err)
    match = _AUTH_URL_RE.search(combined)
    if match:
        return rc == 0, match.group(0), combined.strip()[:4096]
    # No URL captured. If rc==0 we're already authed; if rc!=0 something
    # went wrong but we still let the caller see the output.
    return rc == 0, "", combined.strip()[:4096]
