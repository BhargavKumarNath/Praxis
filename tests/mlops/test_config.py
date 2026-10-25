"""The lifecycle policy is pre-registered: pinned by hash, validated on load."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from praxis.config import Environment
from praxis.domain.model_lifecycle import ModelStage
from praxis.mlops.config import DEFAULT_MLOPS_CONFIG, MlopsConfig, load_mlops_config

ROOT = Path(__file__).resolve().parents[2]
# Changing either file needs an ADR and the owner's approval (CLAUDE.md s16). Re-pin only then.
PINNED = {
    "configs/mlops/demand.toml": (
        # re-pinned 2026-10-25: attempt 2 persistence_days = 2, calibrated on dev seeds 1,
        # 611, 612, 613 only (ADR 0017 "Calibration", owner-delegated authority)
        "ee272c33c2e784699e8099784c261693c7ec20137d7f7d991688317f11b144bb"
    ),
    "configs/mlops/lifecycle_acceptance.toml": (
        # re-pinned 2026-10-25: attempt 2 acceptance (ADR 0017 "Attempt 2")
        "eaa93a837d7227dea973633a4b7bf3856b2e80a78c0533ddaaca6fe1da6bbba4"
    ),
    "configs/simulator/scenarios/mlops_drift.toml": (
        # re-pinned 2026-10-25: attempt 2 drift world (ADR 0017 "Attempt 2")
        "3af67d31f90fb1a9e56d262e3cd6bd16488ce77bbec82f8bd0c7c0a2e8ec2195"
    ),
    "configs/simulator/scenarios/mlops_steady.toml": (
        # pinned 2026-10-25: attempt 2 steady world (ADR 0017 "Attempt 2")
        "3018bfd5f3cb2ef3034413092e109c300258e172a284f0548aa185cbe3952591"
    ),
}


@pytest.mark.parametrize("path", sorted(PINNED))
def test_preregistered_files_are_pinned(path: str) -> None:
    digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    assert digest == PINNED[path], f"{path} changed: needs an ADR + owner approval"


def test_policy_loads_and_keeps_adr_0005() -> None:
    cfg = load_mlops_config()
    assert DEFAULT_MLOPS_CONFIG.is_file()
    lc = cfg.lifecycle
    assert lc.stages == (ModelStage.CHALLENGER, ModelStage.SHADOW, ModelStage.CHAMPION)
    assert lc.champion_requires_approval and lc.auto_advance_to == ModelStage.SHADOW
    assert lc.automatic(ModelStage.SHADOW) and not lc.automatic(ModelStage.CHAMPION)
    assert lc.next_stage(ModelStage.SHADOW) == ModelStage.CHAMPION
    assert lc.next_stage(ModelStage.CHAMPION) is None
    assert lc.next_stage(ModelStage.RETIRED) is None
    assert Environment.PROD not in lc.forced_promotion_environments
    assert "log_scale" not in cfg.monitoring.features  # amended pre-run (ADR 0017)
    assert cfg.policy_version.startswith("mlops-") and cfg.policy_version == (
        load_mlops_config().policy_version
    )


def _raw(**lifecycle: Any) -> dict[str, Any]:
    raw = load_mlops_config().model_dump(mode="json")
    raw["lifecycle"].update(lifecycle)
    return raw


@pytest.mark.parametrize(
    ("lifecycle", "message"),
    [
        ({"stages": ["shadow", "champion"]}, "challenger first"),
        ({"stages": ["challenger", "champion", "shadow"]}, "ordered subsequence"),
        ({"stages": ["challenger", "shadow", "canary", "champion"]}, "canary"),
        ({"auto_advance_to": "canary"}, "one of the stages"),
        ({"auto_advance_to": "champion"}, "contradicts"),
        ({"forced_promotion_environments": ["prod"]}, "never allowed in prod"),
    ],
)
def test_invalid_policies_are_refused(lifecycle: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        MlopsConfig.model_validate(_raw(**lifecycle))


def test_invalid_coverage_band_is_refused() -> None:
    raw = load_mlops_config().model_dump(mode="json")
    raw["shadow"]["coverage_80"] = [0.9, 0.7]
    with pytest.raises(ValidationError, match="coverage band"):
        MlopsConfig.model_validate(raw)
    raw = load_mlops_config().model_dump(mode="json")
    raw["monitoring"]["coverage_80"] = [0.2, 1.2]
    with pytest.raises(ValidationError, match="coverage band"):
        MlopsConfig.model_validate(raw)


def _scenario(name: str) -> dict[str, Any]:
    import tomllib

    return tomllib.loads((ROOT / f"configs/simulator/scenarios/{name}.toml").read_text())


def test_steady_world_is_the_pricing_shadow_world_without_organic_growth() -> None:
    """Every event of the pricing shadow world, unchanged; only per-customer growth is off."""
    base, steady = _scenario("pricing_shadow"), _scenario("mlops_steady")
    assert steady["population"] == {
        **base["population"],
        "growth_mean": 0.0,
        "growth_sd": 0.0001,
    }
    assert {k: v for k, v in steady.items() if k != "population"} == {
        k: v for k, v in base.items() if k != "population"
    }


def test_drift_world_extends_the_steady_world_verbatim() -> None:
    """Days 0..202 must equal the MLOps steady world: its events are copied unchanged."""
    base, drift = _scenario("mlops_steady"), _scenario("mlops_drift")
    assert drift["run"] == base["run"] and drift["population"] == base["population"]
    assert drift["pricing"] == base["pricing"]
    assert drift["infrastructure"]["capacity_shocks"] == base["infrastructure"]["capacity_shocks"]
    spikes = drift["infrastructure"]["demand_spikes"]
    base_spikes = base["infrastructure"]["demand_spikes"]
    assert spikes[: len(base_spikes)] == base_spikes
    assert all(sp["start_day"] >= 203 for sp in spikes[len(base_spikes) :])
