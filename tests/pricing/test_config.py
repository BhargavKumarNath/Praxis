"""Pre-registered pricing configuration: pinned files, validation, the shadow world's lineage."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from praxis.pricing.config import Mode, PricingPolicy, load_policy
from praxis.science.pricing_shadow import load_shadow_config
from praxis.simulator.config import load_config
from praxis.simulator.population import generate_population

REPO = Path(__file__).resolve().parents[2]
SCENARIOS = REPO / "configs/simulator/scenarios"

# Pinned 2026-10-06, before the optimiser ran on any world. A change is a new policy /
# acceptance and needs an ADR (CLAUDE.md s16): never edit these to flatter a result.
PINNED = {
    "configs/pricing/policy.toml": (
        "fd8234d53530f5e63aec028b0cafe1ec821d04a68913470777b619b4f21cf6e0"
    ),
    # re-pinned: ADR 0013 amendment (before seed 42, owner-approved); then a comment-only edit
    # registering seed 43 as the held-out re-evaluation (second amendment; no threshold change)
    "configs/pricing/shadow_acceptance.toml": (
        "fb67283fb760f56a8fb0cac61b07e4dacac1c96ab6e22992596224bc446f7e54"
    ),
    "configs/simulator/scenarios/pricing_shadow.toml": (
        "caca13ea004eeb144c037ae065bdb193411fac6c2eb13f398093fc47d54bb003"
    ),
}


@pytest.mark.parametrize("path", sorted(PINNED))
def test_preregistered_files_are_unchanged(path: str) -> None:
    assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == PINNED[path]


def test_policy_starts_in_shadow_mode_with_execution_disabled() -> None:
    policy = load_policy()
    assert policy.policy.mode is Mode.SHADOW
    assert policy.policy.allow_execute is False
    assert policy.version.startswith("policy-") and len(policy.version) == 19
    assert set(policy.products) == {p.id for p in load_config().products}


def test_policy_version_tracks_content_and_mode() -> None:
    policy = load_policy()
    assert policy.with_mode(Mode.RECOMMEND).version != policy.version
    assert policy.with_mode(Mode.SHADOW).version == policy.version


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("policy", "mode", "execute"),  # execute needs allow_execute
        ("policy", "grid_points", 40),  # even grid
        ("constraints", "max_step", 0.0),
        ("uncertainty", "min_prob_improvement", 0.3),
    ],
)
def test_invalid_policies_are_rejected(section: str, field: str, value: object) -> None:
    raw = tomllib.loads((REPO / "configs/pricing/policy.toml").read_text())
    raw[section][field] = value
    with pytest.raises(ValidationError):
        PricingPolicy.model_validate(raw)


def test_inverted_product_bounds_are_rejected() -> None:
    raw = tomllib.loads((REPO / "configs/pricing/policy.toml").read_text())
    raw["products"]["api_requests"] = {"floor_micros": 10, "ceiling_micros": 5}
    with pytest.raises(ValidationError):
        PricingPolicy.model_validate(raw)


def test_shadow_acceptance_is_loadable_and_matches_the_world() -> None:
    cfg = load_shadow_config()
    world = load_config(scenario=SCENARIOS / "pricing_shadow.toml")
    last = cfg.shadow.first_cycle_day + cfg.shadow.cycles * cfg.shadow.cycle_days
    assert last == world.run.days  # every cycle is fully observed
    assert world.population.arrival_horizon_days == cfg.shadow.first_cycle_day


def test_shadow_world_is_the_forecast_world_continued() -> None:
    """Same overlay content apart from the run length and the arrival horizon."""
    forecast = tomllib.loads((SCENARIOS / "forecast_eval.toml").read_text())
    shadow = tomllib.loads((SCENARIOS / "pricing_shadow.toml").read_text())
    assert shadow.pop("population") == {"arrival_horizon_days": 196}
    assert shadow.pop("run") == {"days": 252}
    forecast.pop("run")
    assert shadow == forecast


def test_shadow_world_shares_the_forecast_worlds_population() -> None:
    a = load_config(scenario=SCENARIOS / "forecast_eval.toml").with_overrides(n_customers=300)
    b = load_config(scenario=SCENARIOS / "pricing_shadow.toml").with_overrides(n_customers=300)
    pa, pb = generate_population(a, 42), generate_population(b, 42)
    for name in ("created_day", "elasticity", "mix", "base_load", "churn_sens", "tier"):
        assert np.array_equal(getattr(pa, name), getattr(pb, name)), name


def test_arrival_horizon_is_hash_neutral_when_unset() -> None:
    world = load_config()
    assert "arrival_horizon_days" not in world.canonical_json()
    shifted = world.model_copy(
        update={"population": world.population.model_copy(update={"arrival_horizon_days": 30})}
    )
    assert shifted.config_hash != world.config_hash
