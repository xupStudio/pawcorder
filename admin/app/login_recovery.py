"""Login recovery via a file-flag.

A user with shell / file-manager access to the Pawcorder data directory
can drop an empty marker file at ``$PAWCORDER_DATA_DIR/config/.reset-password``.
The login page detects the marker and offers an inline "set a new
password" form — no admin authentication required. The flag is removed
after a successful reset.

Security model: same as the existing "edit .env on the host" recovery
path. Anyone who can write to the data dir already has full control of
the install. The file-flag just makes recovery discoverable for the
non-engineer family member that shares the box.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from . import config_store, users as users_mod

logger = logging.getLogger("pawcorder.login_recovery")

FLAG_NAME = ".reset-password"


def _flag_path() -> Path:
    """Resolved at call time so test fixtures swapping
    ``PAWCORDER_DATA_DIR`` per-fixture work correctly."""
    return Path(os.environ.get("PAWCORDER_DATA_DIR", "/data")) / "config" / FLAG_NAME


def is_armed() -> bool:
    return _flag_path().exists()


def disarm() -> None:
    """Delete the flag. Called after a successful reset."""
    p = _flag_path()
    try:
        if p.exists():
            p.unlink()
    except OSError as exc:  # non-critical UX — log and move on
        logger.warning("could not remove reset flag at %s: %s", p, exc)


def reset_password(new_password: str) -> None:
    """Reset the admin password.

    For single-password (legacy) installs: rewrites ADMIN_PASSWORD in .env.
    For multi-user installs: rewrites the first admin user's hash. We
    deliberately don't enumerate / let the user pick a username here — the
    intent of file-flag recovery is "I'm locked out, give me a single
    way back in".
    """
    if not new_password:
        raise ValueError("password must not be empty")
    if len(new_password) < 4:
        raise ValueError("password too short")

    if users_mod.has_users():
        # Multi-user — find the first admin and reset its hash.
        admin = next((u for u in users_mod.list_users() if u.role == "admin"), None)
        if admin is None:
            raise RuntimeError("multi-user mode but no admin user exists")
        users_mod.change_password(admin.username, new_password)
    else:
        cfg = config_store.load_config()
        cfg.admin_password = new_password
        config_store.save_config(cfg)
