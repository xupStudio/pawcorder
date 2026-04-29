"""Tests for the i18n lookup."""
from __future__ import annotations

import pytest

from app.i18n import T, make_translator, t, SUPPORTED, DEFAULT_LANG


def test_default_lang_known():
    assert DEFAULT_LANG in SUPPORTED


def test_t_returns_lang_specific_string():
    assert t("NAV_DASHBOARD", "en") == "Dashboard"
    assert t("NAV_DASHBOARD", "zh-TW") == "儀表板"


def test_t_falls_back_to_english_for_partial_translation():
    # Manufacture a key only in 'en'.
    T.setdefault("__test_partial__", {"en": "fallback"})
    try:
        assert t("__test_partial__", "zh-TW") == "fallback"
    finally:
        del T["__test_partial__"]


def test_t_falls_back_to_key_when_missing():
    assert t("__definitely_missing_key__", "en") == "__definitely_missing_key__"


def test_make_translator_closure():
    tr = make_translator("zh-TW")
    assert tr("NAV_CAMERAS") == "攝影機"


def test_all_keys_have_at_least_english():
    missing = [k for k, v in T.items() if "en" not in v]
    assert not missing, f"keys missing English: {missing}"


def test_all_keys_have_zh_tw():
    """We've committed to full bilingual coverage."""
    missing = [k for k, v in T.items() if "zh-TW" not in v]
    assert not missing, f"keys missing zh-TW: {missing}"
