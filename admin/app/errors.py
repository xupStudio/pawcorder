"""User-friendly error envelope.

Backend code raises ``UserError`` objects (or returns them in a list).
The dispatcher renders them into ``{title, body, fix, severity, fix_action}``
JSON the dashboard / banners can show without leaking stack traces.

Adding a new error:
    1. Add ``ERR_<NAME>_TITLE`` / ``BODY`` / ``FIX`` keys to i18n.py.
    2. Define a factory here (``camera_offline(name)``).
    3. Call the factory from the place that detects the condition.

The dashboard's ``/api/diagnostics`` endpoint aggregates the current
list. Errors with the same ``code`` collapse — repeated camera-offline
events for one camera show as one entry, not one per probe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import i18n


@dataclass(frozen=True)
class UserError:
    code: str
    title_key: str
    body_key: str
    fix_key: str
    severity: str = "warn"
    fix_action: str | None = None
    fix_label_key: str | None = None
    fmt: dict[str, Any] = field(default_factory=dict)
    diagnostic: dict[str, Any] = field(default_factory=dict)

    def render(self, lang: str = "zh-TW") -> dict[str, Any]:
        def _t(key: str) -> str:
            s = i18n.t(key, lang=lang)
            if not self.fmt:
                return s
            for k, v in self.fmt.items():
                s = s.replace("{" + k + "}", str(v))
            return s

        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "title": _t(self.title_key),
            "body": _t(self.body_key),
            "fix": _t(self.fix_key),
        }
        if self.fix_action:
            out["fix_action"] = self.fix_action
            out["fix_label"] = _t(self.fix_label_key or "ERR_RESTART_BUTTON")
        if self.diagnostic:
            out["diagnostic"] = self.diagnostic
        return out


# ---- factories -----------------------------------------------------------


def frigate_down(*, log_excerpt: str | None = None) -> UserError:
    return UserError(
        code="frigate_down",
        title_key="ERR_FRIGATE_DOWN_TITLE",
        body_key="ERR_FRIGATE_DOWN_BODY",
        fix_key="ERR_FRIGATE_DOWN_FIX",
        severity="error",
        fix_action="/api/system/restart-frigate",
        fix_label_key="ERR_RESTART_BUTTON",
        diagnostic={"service": "frigate", "log_tail": log_excerpt or ""},
    )


def camera_offline(name: str, *, last_seen: str | None = None) -> UserError:
    return UserError(
        code=f"camera_offline:{name}",
        title_key="ERR_CAMERA_OFFLINE_TITLE",
        body_key="ERR_CAMERA_OFFLINE_BODY",
        fix_key="ERR_CAMERA_OFFLINE_FIX",
        severity="warn",
        fmt={"name": name},
        diagnostic={"camera": name, "last_seen": last_seen or "never"},
    )


def disk_full(*, free_pct: float, free_bytes: int) -> UserError:
    return UserError(
        code="disk_full",
        title_key="ERR_DISK_FULL_TITLE",
        body_key="ERR_DISK_FULL_BODY",
        fix_key="ERR_DISK_FULL_FIX",
        severity="warn" if free_pct > 0.02 else "error",
        diagnostic={"free_pct": round(free_pct, 4), "free_bytes": free_bytes},
    )


def network_down(*, last_outbound: str | None = None) -> UserError:
    return UserError(
        code="network_down",
        title_key="ERR_NETWORK_DOWN_TITLE",
        body_key="ERR_NETWORK_DOWN_BODY",
        fix_key="ERR_NETWORK_DOWN_FIX",
        severity="info",
        diagnostic={"last_outbound": last_outbound or "unknown"},
    )


# ---- aggregation ---------------------------------------------------------


def dedupe(errors: list[UserError]) -> list[UserError]:
    """Keep first occurrence per code; preserves insertion order."""
    seen: set[str] = set()
    out: list[UserError] = []
    for e in errors:
        if e.code in seen:
            continue
        seen.add(e.code)
        out.append(e)
    return out


def render_all(errors: list[UserError], lang: str = "zh-TW") -> list[dict[str, Any]]:
    return [e.render(lang) for e in dedupe(errors)]
