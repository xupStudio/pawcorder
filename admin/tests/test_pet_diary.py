"""Tests for the LLM pet diary."""
from __future__ import annotations

import time

import pytest


def _pet(name: str = "Mochi", species: str = "cat"):
    from app.pets_store import Pet
    return Pet(pet_id=name.lower(), name=name, species=species)


def _cfg(**kw):
    from app.config_store import Config
    return Config(**kw)


def _sighting(*, hour: int, pet_id: str = "mochi", camera: str = "front",
              confidence: str = "high", now: float | None = None) -> dict:
    base = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(base))
    ts = time.mktime(time.strptime(f"{today} {hour:02d}:00", "%Y-%m-%d %H:%M"))
    return {
        "event_id": f"evt-{hour}",
        "camera": camera,
        "label": "cat",
        "pet_id": pet_id,
        "pet_name": "Mochi",
        "score": 0.92,
        "confidence": confidence,
        "start_time": ts,
        "end_time": ts + 5,
    }


# ---- summary builder ---------------------------------------------------

def test_build_summary_empty_sightings_returns_zeroed_summary(data_dir):
    from app import pet_diary
    s = pet_diary.build_summary(_pet(), sightings=[])
    assert s.sightings == 0
    assert s.cameras == []
    assert s.peak_hours == []
    assert s.last_seen_hour is None


def test_build_summary_aggregates_cameras_and_hours(data_dir):
    from app import pet_diary
    now = time.time()
    rows = [
        _sighting(hour=8,  camera="front", now=now),
        _sighting(hour=8,  camera="front", now=now),
        _sighting(hour=8,  camera="back",  now=now),
        _sighting(hour=14, camera="front", now=now),
        _sighting(hour=22, camera="back",  now=now, confidence="tentative"),
    ]
    s = pet_diary.build_summary(_pet(), sightings=rows, now=now)
    assert s.sightings == 5
    assert s.cameras[0] == "front"
    assert "back" in s.cameras
    assert 8 in s.peak_hours
    assert s.high_confidence_count == 4
    assert s.last_seen_hour == 22


# ---- prompt builder ----------------------------------------------------

def test_compose_prompt_uses_zh_system_for_zh_lang(data_dir):
    from app import pet_diary
    s = pet_diary.build_summary(_pet(), sightings=[_sighting(hour=8)])
    sys_en, _ = pet_diary.compose_prompt(s, lang="en")
    sys_zh, _ = pet_diary.compose_prompt(s, lang="zh-TW")
    assert sys_en != sys_zh
    assert "first person" in sys_en
    assert "第一人稱" in sys_zh


def test_compose_prompt_includes_pet_name_and_stats(data_dir):
    from app import pet_diary
    s = pet_diary.build_summary(_pet(name="Maru"), sightings=[_sighting(hour=8)])
    _, user = pet_diary.compose_prompt(s, lang="en")
    assert "Maru" in user
    assert "Sightings: 1" in user
    assert "front" in user


# ---- generate_diary backend selection ----------------------------------

@pytest.mark.asyncio
async def test_generate_diary_picks_openai_when_key_set(data_dir):
    from app import pet_diary
    cfg = _cfg(openai_api_key="sk-fake")
    captured = {}

    async def fake_openai(api_key, system, user, **kw):
        captured["key"] = api_key
        captured["user"] = user
        return "Today I purred a lot."

    async def fake_relay(*a, **kw):
        raise AssertionError("relay should not be called when openai key is set")

    d = await pet_diary.generate_diary(
        _pet(), cfg=cfg, sightings=[_sighting(hour=8)],
        openai_caller=fake_openai, relay_caller=fake_relay,
    )
    assert d.backend == "openai"
    assert d.text == "Today I purred a lot."
    assert captured["key"] == "sk-fake"


@pytest.mark.asyncio
async def test_generate_diary_falls_back_to_relay_when_only_license(data_dir):
    from app import pet_diary
    cfg = _cfg(pawcorder_pro_license_key="pro_xyz")

    async def fake_openai(*a, **kw):
        raise AssertionError("openai should not be called when only license is set")

    async def fake_relay(license_key, system, user, *, lang, **kw):
        assert license_key == "pro_xyz"
        return "從貓砂回來，今天舒服。"

    d = await pet_diary.generate_diary(
        _pet(), lang="zh-TW", cfg=cfg, sightings=[_sighting(hour=10)],
        openai_caller=fake_openai, relay_caller=fake_relay,
    )
    assert d.backend == "pro_relay"
    assert "今天" in d.text
    assert d.lang == "zh-TW"


@pytest.mark.asyncio
async def test_generate_diary_raises_when_neither_backend_configured(data_dir):
    from app import pet_diary
    with pytest.raises(pet_diary.DiaryNotConfigured):
        await pet_diary.generate_diary(_pet(), cfg=_cfg(), sightings=[])


@pytest.mark.asyncio
async def test_openai_takes_precedence_over_pro_license(data_dir):
    """If a user has BOTH (OpenAI key + Pro license), respect the
    explicit OpenAI key — they opted into paying directly."""
    from app import pet_diary
    cfg = _cfg(openai_api_key="sk-x", pawcorder_pro_license_key="pro_y")
    seen = []

    async def fake_openai(*a, **kw):
        seen.append("openai")
        return "ok"

    async def fake_relay(*a, **kw):
        seen.append("relay")
        return "ok"

    await pet_diary.generate_diary(
        _pet(), cfg=cfg, sightings=[],
        openai_caller=fake_openai, relay_caller=fake_relay,
    )
    assert seen == ["openai"]


# ---- persistence -------------------------------------------------------

def test_append_diary_overwrites_same_pet_same_day(data_dir):
    from app import pet_diary
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="mochi", pet_name="Mochi", date="2026-04-29",
        text="first", backend="openai", generated_at=1.0,
    ))
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="mochi", pet_name="Mochi", date="2026-04-29",
        text="second", backend="openai", generated_at=2.0,
    ))
    rows = pet_diary.read_diaries(pet_id="mochi")
    assert len(rows) == 1
    assert rows[0]["text"] == "second"


def test_append_diary_keeps_separate_entries_for_different_days(data_dir):
    from app import pet_diary
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="mochi", pet_name="Mochi", date="2026-04-28",
        text="yesterday", backend="openai", generated_at=1.0,
    ))
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="mochi", pet_name="Mochi", date="2026-04-29",
        text="today", backend="openai", generated_at=2.0,
    ))
    rows = pet_diary.read_diaries(pet_id="mochi")
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-04-29"
    assert rows[1]["date"] == "2026-04-28"


def test_read_diaries_filters_by_pet(data_dir):
    from app import pet_diary
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="mochi", pet_name="Mochi", date="2026-04-29",
        text="m", backend="openai", generated_at=1.0,
    ))
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="maru", pet_name="Maru", date="2026-04-29",
        text="r", backend="openai", generated_at=2.0,
    ))
    assert len(pet_diary.read_diaries(pet_id="mochi")) == 1
    assert len(pet_diary.read_diaries(pet_id="maru")) == 1
    assert len(pet_diary.read_diaries()) == 2


def test_read_diaries_handles_missing_log(data_dir):
    from app import pet_diary
    assert pet_diary.read_diaries() == []


def test_read_diaries_skips_malformed_lines(data_dir):
    from app import pet_diary
    pet_diary.append_diary(pet_diary.Diary(
        pet_id="mochi", pet_name="Mochi", date="2026-04-29",
        text="ok", backend="openai", generated_at=1.0,
    ))
    with pet_diary.DIARIES_LOG.open("a", encoding="utf-8") as f:
        f.write("not-json-at-all\n")
    rows = pet_diary.read_diaries()
    assert len(rows) == 1
    assert rows[0]["text"] == "ok"
