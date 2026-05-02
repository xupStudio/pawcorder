"""User-friendly error envelope (errors.UserError)."""
from app import errors


def test_render_includes_translated_strings():
    err = errors.frigate_down(log_excerpt="boom")
    out = err.render(lang="zh-TW")
    # i18n key was looked up — not the bare ALL_CAPS_KEY string.
    assert "影像服務" in out["title"] or "服務" in out["title"]
    assert out["fix_action"] == "/api/system/restart-frigate"
    assert out["fix_label"]
    assert out["diagnostic"]["log_tail"] == "boom"
    assert out["severity"] == "error"


def test_render_substitutes_format_args():
    err = errors.camera_offline("kitchen")
    zh = err.render(lang="zh-TW")
    en = err.render(lang="en")
    assert "kitchen" in zh["title"]
    assert "kitchen" in en["title"]
    assert "{name}" not in zh["title"]
    assert "{name}" not in en["title"]


def test_disk_full_severity_escalates_under_2pct():
    warn = errors.disk_full(free_pct=0.04, free_bytes=1024**3)
    err = errors.disk_full(free_pct=0.01, free_bytes=512 * 1024**2)
    assert warn.severity == "warn"
    assert err.severity == "error"


def test_dedupe_keeps_first_per_code():
    a = errors.camera_offline("kitchen")
    b = errors.camera_offline("kitchen")  # same code
    c = errors.camera_offline("front")    # different name → different code
    deduped = errors.dedupe([a, b, c])
    assert len(deduped) == 2
    assert deduped[0] is a
    assert deduped[1] is c


def test_render_all_returns_json_shape():
    out = errors.render_all([errors.frigate_down(), errors.disk_full(free_pct=0.01, free_bytes=0)])
    assert all(isinstance(x, dict) for x in out)
    assert all({"code", "title", "body", "fix", "severity"} <= x.keys() for x in out)
