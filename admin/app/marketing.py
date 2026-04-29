"""Email signup collection for the pre-launch landing page.

Endpoint is public (no auth, no CSRF) so the marketing page can post
to it from any origin. We rate-limit by IP to discourage scripted
spam, validate the email shape minimally, and append to a CSV that the
admin can download via /api/marketing/signups (auth-required).

The CSV is the source of truth — no DB. Fields:

  timestamp_iso, email, source, locale, ip

`source` lets future marketing pages tag the signup ("landing", "pro",
"newsletter") so we can split the funnel later.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pawcorder.marketing")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
SIGNUPS_CSV = DATA_DIR / "config" / "email_signups.csv"
CSV_HEADER = ["timestamp_iso", "email", "source", "locale", "ip"]

# RFC-5321 max local-part is 64, max total is 254. We don't accept
# disposable-mail patterns; that's a future concern.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MAX_EMAIL_LEN = 254
MAX_SOURCE_LEN = 32

# In-memory IP rate limit. Coarse — single-process — but enough to
# discourage trivial spam bots. Shared with no other code.
_rate_lock = threading.Lock()
_recent: dict[str, list[float]] = {}
RATE_LIMIT_PER_IP = 5      # signups
RATE_LIMIT_WINDOW = 3600.0 # seconds

# Serializes the read-decide-write window in record_signup so two
# concurrent submissions of the same email cannot both miss the dup
# check and double-append. Cheap — at most one signup at a time across
# the whole process, which is fine at our load.
_write_lock = threading.Lock()


@dataclass
class SignupResult:
    ok: bool
    error: str = ""
    duplicate: bool = False  # already in CSV from this email
    rate_limited: bool = False


def _normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def _normalize_source(raw: str) -> str:
    s = (raw or "landing").strip().lower()
    s = re.sub(r"[^a-z0-9_\-]+", "", s)
    return s[:MAX_SOURCE_LEN] or "landing"


def _is_valid_email(email: str) -> bool:
    if not email or len(email) > MAX_EMAIL_LEN:
        return False
    return bool(EMAIL_RE.match(email))


def _rate_limited(ip: str) -> bool:
    """Returns True if this IP has hit the per-window cap."""
    if not ip:
        return False  # missing IP info — don't punish, just log later
    now = time.time()
    with _rate_lock:
        bucket = _recent.setdefault(ip, [])
        # Drop expired timestamps.
        bucket[:] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
        if len(bucket) >= RATE_LIMIT_PER_IP:
            return True
        bucket.append(now)
    return False


def _existing_emails() -> set[str]:
    if not SIGNUPS_CSV.exists():
        return set()
    out: set[str] = set()
    try:
        with SIGNUPS_CSV.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                e = (row.get("email") or "").strip().lower()
                if e:
                    out.add(e)
    except (OSError, csv.Error):
        logger.warning("could not read signups CSV; proceeding with empty set")
    return out


def record_signup(*, email: str, source: str = "landing", locale: str = "",
                  ip: str = "") -> SignupResult:
    """Validate + persist one signup. Pure function — no FastAPI deps.

    The read-decide-write window is held under _write_lock so two
    concurrent signups of the same email can't both miss the dup
    check and write twice.
    """
    email = _normalize_email(email)
    if not _is_valid_email(email):
        return SignupResult(ok=False, error="invalid email")
    if _rate_limited(ip):
        return SignupResult(ok=False, error="too many signups", rate_limited=True)

    with _write_lock:
        if email in _existing_emails():
            # Idempotent — return ok with a duplicate flag so the UI
            # can say "already on the list, thanks!" without leaking
            # that it's a dup.
            return SignupResult(ok=True, duplicate=True)

        SIGNUPS_CSV.parent.mkdir(parents=True, exist_ok=True)
        is_new = not SIGNUPS_CSV.exists()
        try:
            with SIGNUPS_CSV.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
                if is_new:
                    writer.writeheader()
                writer.writerow({
                    "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "email": email,
                    "source": _normalize_source(source),
                    "locale": (locale or "").strip()[:16],
                    "ip": ip[:64],
                })
        except OSError as exc:
            logger.warning("failed to write signup: %s", exc)
            return SignupResult(ok=False, error="storage error")
    return SignupResult(ok=True)


def list_signups(*, limit: int = 1000) -> list[dict]:
    """Read the CSV back for the admin export endpoint. Newest last."""
    if not SIGNUPS_CSV.exists():
        return []
    out: list[dict] = []
    try:
        with SIGNUPS_CSV.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                out.append(dict(row))
    except (OSError, csv.Error):
        return []
    return out[-limit:]


def reset_rate_limits() -> None:
    """Test helper — wipe the in-memory bucket between tests."""
    with _rate_lock:
        _recent.clear()
