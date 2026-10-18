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
        # re-pinned 2026-10-17: champion non-inferiority amendment (ADR 0017, owner-approved)
        "d7dcb87d8e6ad44fae1926791e0f973e8e8b6abfdfba24a81efe30743b75abd3"
    ),
    "configs/mlops/lifecycle_acceptance.toml": (
        "355b759f982a83060f9cad0194a370237772ea8344a52b417dd7ff713f6b6010"
    ),
    "configs/simulator/scenarios/mlops_drift.toml": (
        "705cb7cc769855d4010f4f2cbf48a70daea6836ca09b883fc4e566847aa31214"
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


def test_drift_world_extends_the_pricing_shadow_world_verbatim() -> None:
    """Days 0..202 must equal the pricing shadow world: its events are copied unchanged."""
    import tomllib

    base = tomllib.loads((ROOT / "configs/simulator/scenarios/pricing_shadow.toml").read_text())
    drift = tomllib.loads((ROOT / "configs/simulator/scenarios/mlops_drift.toml").read_text())
    assert drift["run"] == base["run"] and drift["population"] == base["population"]
    assert drift["pricing"] == base["pricing"]
    assert drift["infrastructure"]["capacity_shocks"] == base["infrastructure"]["capacity_shocks"]
    spikes = drift["infrastructure"]["demand_spikes"]
    base_spikes = base["infrastructure"]["demand_spikes"]
    assert spikes[: len(base_spikes)] == base_spikes
    assert all(sp["start_day"] >= 203 for sp in spikes[len(base_spikes) :])
