"""Federated baseline client — opt-in cohort stats.

The user toggles this on under System → Privacy. Once a week the
admin sends the relay an anonymised summary of each pet's last
7 days (sightings count + hour histogram + species + age band) and
gets back the cohort mean / std for "cats this age". The /pets
page can then show "Mochi is 1.4× the cohort average — chatty cat!"
or "Mochi is unusually quiet today vs. her cohort", which is a
much sharper signal than the within-pet baseline alone (because the
within-pet baseline drifts in lockstep with the pet's own decline).

Privacy guarantees the user gets:

  * Only species, coarse age band, sighting counts, and hour-bucket
    proportions are sent. NO snapshots, NO camera names, NO times,
    NO pet names.
  * The opt-in is strictly explicit — default is OFF.
  * The relay's per-license daily quota means a leaked license can't
    flood-poison the cohort (DAILY_SUBMIT_QUOTA=5).

Pro-only path (the relay endpoint requires a license key). OSS users
without a license see the toggle disabled with a "needs Pro" note.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import config_store, recognition
from .pets_store import Pet, PetStore
from .utils import PollingTask


def _data_dir() -> Path:
    """Resolved at call-time so tests can override PAWCORDER_DATA_DIR
    after this module has imported."""
    return Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))

logger = logging.getLogger("pawcorder.federated")

PRO_RELAY_BASE = os.environ.get(
    "PAWCORDER_RELAY_BASE", "https://relay.pawcorder.app"
).rstrip("/")
SUBMIT_PATH = "/v1/baseline/submit"
COHORT_PATH = "/v1/baseline/cohort"

# Submission cadence — once a week, slow signal, no point hitting the
# relay more often than the cohort is going to update.
SUBMIT_INTERVAL_SECONDS = 7 * 86400


@dataclass
class SubmissionPayload:
    """What we send to the relay. Bound to the wire format — keep small."""
    species: str
    age_band: str
    daily_counts: list[float] = field(default_factory=list)
    hour_histogram: list[float] = field(default_factory=list)

    def to_dict(self, license_key: str) -> dict:
        return {
            "license_key": license_key,
            "species": self.species,
            "age_band": self.age_band,
            "daily_counts": self.daily_counts,
            "hour_histogram": self.hour_histogram,
        }


def build_submission(pet: Pet, *, now: Optional[float] = None,
                      sightings: Optional[list[dict]] = None) -> SubmissionPayload:
    """Bucket the last 7 days of sightings into the cohort-friendly shape.

    Caller can pass an explicit `sightings` list so the unit tests
    don't need to monkey-patch the recognition module.
    """
    now = now or time.time()
    if sightings is None:
        sightings = recognition.read_sightings(
            limit=10_000, since=now - 7 * 86400,
        )
    rows = [r for r in sightings if r.get("pet_id") == pet.pet_id]

    # 7 daily counts (most recent last).
    daily_counts: list[float] = []
    for i in range(6, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        cnt = sum(
            1 for r in rows
            if time.strftime("%Y-%m-%d", time.localtime(
                float(r.get("start_time") or 0)
            )) == day
        )
        daily_counts.append(float(cnt))

    # 24-bin hour histogram across the whole window. Normalised in
    # the relay; we send raw counts so the relay's normalisation is
    # the single source of truth.
    hist = [0.0] * 24
    for r in rows:
        ts = float(r.get("start_time") or 0)
        if ts <= 0:
            continue
        hist[time.localtime(ts).tm_hour] += 1.0

    return SubmissionPayload(
        species=pet.species,
        age_band=_age_band_for(pet),
        daily_counts=daily_counts,
        hour_histogram=hist,
    )


def _age_band_for(pet: Pet) -> str:
    """Coarse age band from the pet's free-text notes / default 'unknown'.

    We don't ask the user to enter age explicitly — the notes field is
    free-form. If they've written "kitten" / "senior" anywhere we lift
    the band; otherwise we default to 'unknown' (which forms its own
    cohort). Deliberately coarse — passing exact age would be a re-id
    risk for niche breeds.
    """
    notes = (pet.notes or "").lower()
    if any(w in notes for w in ("kitten", "puppy", "幼", "幼貓", "幼犬")):
        return "kitten"
    if any(w in notes for w in ("senior", "elderly", "高齡", "老貓", "老犬")):
        return "senior"
    if any(w in notes for w in ("adult", "成貓", "成犬")):
        return "adult"
    return "unknown"


# ---- relay calls -------------------------------------------------------

class FederatedDisabled(RuntimeError):
    """Either the user hasn't opted in, or no Pro license is set."""


async def submit_for_pet(pet: Pet, license_key: str, *,
                          base: Optional[str] = None,
                          now: Optional[float] = None,
                          submit_caller=None) -> dict:
    """POST one pet's submission to the relay. Returns the decoded
    JSON response (which includes the cohort) on success.

    `submit_caller` is the DI seam tests use to skip the network.
    """
    payload = build_submission(pet, now=now)
    body = payload.to_dict(license_key)
    if submit_caller is not None:
        return await submit_caller(body)
    base = (base or PRO_RELAY_BASE).rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(base + SUBMIT_PATH, json=body)
    if resp.status_code == 401:
        raise RuntimeError("federated: license invalid")
    if resp.status_code == 429:
        raise RuntimeError("federated: quota exceeded")
    if resp.status_code != 200:
        raise RuntimeError(
            f"federated submit http {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


async def fetch_cohort(license_key: str, species: str, age_band: str = "unknown",
                        *, base: Optional[str] = None,
                        cohort_caller=None) -> dict:
    """GET the cohort mean / std for (species, age_band)."""
    if cohort_caller is not None:
        return await cohort_caller(license_key, species, age_band)
    base = (base or PRO_RELAY_BASE).rstrip("/")
    params = {
        "license_key": license_key,
        "species": species,
        "age_band": age_band,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(base + COHORT_PATH, params=params)
    if resp.status_code == 401:
        raise RuntimeError("federated: license invalid")
    if resp.status_code != 200:
        raise RuntimeError(
            f"federated cohort http {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


# ---- scheduler ---------------------------------------------------------

class FederatedScheduler(PollingTask):
    """Once a week (~hour granularity), if the user is opted in and has
    a Pro license, submit each pet's stats and stash the cohort
    response in /data/config/federated_cohorts.json for the UI."""

    name = "federated-baseline-scheduler"
    interval_seconds = 3600.0   # check hourly; submit weekly

    LAST_RUN_FIELD = "_last_submit_ts"

    async def _tick(self) -> None:
        cfg = config_store.load_config()
        if not cfg.federated_opt_in:
            return
        if not cfg.pawcorder_pro_license_key:
            return
        # Manual cooldown: avoid the polling-task interval being too
        # tight (we only want one weekly submission, not 168).
        from .utils import atomic_write_text
        DATA_DIR_NOW = _data_dir()
        state_path = DATA_DIR_NOW / "config" / "federated_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        last = float(state.get(self.LAST_RUN_FIELD) or 0)
        now = time.time()
        if now - last < SUBMIT_INTERVAL_SECONDS:
            return

        cohorts_path = DATA_DIR_NOW / "config" / "federated_cohorts.json"
        cohorts: dict = {}
        for pet in PetStore().load():
            try:
                resp = await submit_for_pet(pet, cfg.pawcorder_pro_license_key)
                cohort = resp.get("cohort") or {}
                if cohort.get("sample_size", 0) > 0:
                    cohorts[f"{cohort['species']}:{cohort['age_band']}"] = cohort
            except Exception as exc:  # noqa: BLE001
                logger.warning("federated submit failed for %s: %s",
                                pet.pet_id, exc)
        state[self.LAST_RUN_FIELD] = now
        try:
            # Atomic — a kill mid-write previously left the state file
            # truncated, which made the scheduler treat the next tick
            # as the first run and re-submit (= burning relay quota
            # for nothing). Same pattern every other persistent file
            # in the admin uses.
            atomic_write_text(state_path, json.dumps(state))
            if cohorts:
                atomic_write_text(
                    cohorts_path,
                    json.dumps(cohorts, ensure_ascii=False),
                )
        except OSError as exc:
            logger.warning("federated state write failed: %s", exc)


def read_cached_cohorts() -> dict:
    """Read whatever the last weekly submit fetched. Used by the /pets
    page to render comparison badges without a network round-trip per
    page load."""
    p = _data_dir() / "config" / "federated_cohorts.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


scheduler = FederatedScheduler()
