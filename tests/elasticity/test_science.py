"""Ground-truth recovery evaluation (praxis.science): truth estimand, every check can fail."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from praxis.elasticity.analysis import Analysis
from praxis.science.__main__ import main as science_main
from praxis.science.elasticity_recovery import (
    Truth,
    TruthError,
    UnitTruth,
    evaluate,
    evaluate_contamination,
    evaluate_recovery,
    load_acceptance,
)
from praxis.simulator.config import TIERS, SimulationConfig, load_config
from tests.elasticity.helpers import INDUSTRIES, true_elasticity
from tests.elasticity.helpers import TIERS as HELPER_TIERS

N = 3000
ACC = load_acceptance()


def _world() -> SimulationConfig:
    return load_config().with_overrides(n_customers=N)


def _write_truth(path: Path, world: SimulationConfig, *, shift: float = 0.0) -> Path:
    industries = [i.id for i in world.industries]
    tier = np.array([TIERS.index(HELPER_TIERS[i % 3]) for i in range(N)], dtype=np.int8)
    industry = np.array(
        [industries.index(INDUSTRIES[(i // 3) % 3]) for i in range(N)], dtype=np.int8
    )
    elasticity = np.array(
        [true_elasticity(HELPER_TIERS[i % 3], INDUSTRIES[(i // 3) % 3]) + shift for i in range(N)]
    )
    np.savez(
        path, elasticity=elasticity, tier=tier, industry=industry, config_hash=world.config_hash
    )
    return path


def _rows(a: Analysis) -> list[dict[str, Any]]:
    return [
        {**r, "in_model": bool(f)}
        for r, f in zip(a.units.records(), a.in_model.tolist(), strict=True)
    ]


@pytest.fixture(scope="module")
def truth(tmp_path_factory: pytest.TempPathFactory) -> Truth:
    world = _world()
    return Truth.load(_write_truth(tmp_path_factory.mktemp("t") / "gt.npz", world), world)


def _by_name(results: list[Any]) -> dict[str, Any]:
    return {r.name: r for r in results}


def test_clean_synthetic_world_passes_every_recovery_check(
    analysis: Analysis, truth: Truth
) -> None:
    out = evaluate(analysis.report, _rows(analysis), truth, _world(), ACC)
    assert out["mode"] == "recovery"
    assert {c["name"] for c in out["checks"]} == {
        "sign",
        "pooled_magnitude",
        "tier_magnitude",
        "segment_ordering",
        "cell_interval_coverage",
        "pooling_justified",
        "validity_gates",
        "bayesian_diagnostics",
    }
    assert out["passed"], [c for c in out["checks"] if not c["passed"]]
    assert out["notice"].startswith("SYNTHETIC")
    assert set(out["failure_cases"]["dose"]) == {"raise", "cut"}


def test_truth_is_the_design_weighted_mean_over_the_analysed_units(
    analysis: Analysis, truth: Truth
) -> None:
    units = UnitTruth.join(_rows(analysis), truth)
    m = units.observed
    assert units.mean(m) == pytest.approx(
        float(np.average(units.elasticity[m], weights=units.weight[m]))
    )
    cells = units.cells()
    assert cells["starter|media"] == pytest.approx(true_elasticity("starter", "media"))


Mutation = Callable[[dict[str, Any]], None]


def _set(path: list[str], value: Any) -> Mutation:
    def mutate(r: dict[str, Any]) -> None:
        node = r
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


def _scale_pooled(r: dict[str, Any]) -> None:
    r["estimates"]["pooled"]["estimate"] *= 1.3


def _swap_tiers(r: dict[str, Any]) -> None:
    for pair in r["hierarchical"]["pairwise"]["tier"]:
        pair["mean_difference"] = -pair["mean_difference"]
        pair["p_a_less_than_b"] = 1.0 - pair["p_a_less_than_b"]


def _narrow_cells(r: dict[str, Any]) -> None:
    for c in r["hierarchical"]["cells"].values():
        c["interval_low"] = c["interval_high"] = c["mean"] + 5.0


def _worse_pooling(r: dict[str, Any]) -> None:
    for c in r["hierarchical"]["cells"].values():
        c["mean"] += 1.0


def _fail_validity(r: dict[str, Any]) -> None:
    r["validity"]["checks"][0]["passed"] = False


def _scale_tier(r: dict[str, Any]) -> None:
    r["hierarchical"]["tier"]["enterprise"]["mean"] *= 1.5


@pytest.mark.parametrize(
    ("check", "mutate"),
    [
        ("sign", _set(["estimates", "pooled", "ci_high"], 0.1)),
        ("sign", _set(["hierarchical", "tier", "enterprise", "ci95_high"], 0.2)),
        ("pooled_magnitude", _scale_pooled),
        ("tier_magnitude", _scale_tier),
        ("segment_ordering", _swap_tiers),
        ("cell_interval_coverage", _narrow_cells),
        ("pooling_justified", _worse_pooling),
        ("validity_gates", _fail_validity),
        ("bayesian_diagnostics", _set(["hierarchical", "diagnostics", "passed"], False)),
    ],
)
def test_each_preregistered_check_can_fail(
    analysis: Analysis, truth: Truth, check: str, mutate: Mutation
) -> None:
    report = copy.deepcopy(analysis.report)
    mutate(report)
    results = _by_name(
        evaluate_recovery(report, UnitTruth.join(_rows(analysis), truth), ACC.recovery)
    )
    assert not results[check].passed


def test_pooled_magnitude_also_needs_statistical_agreement(
    analysis: Analysis, truth: Truth
) -> None:
    report = copy.deepcopy(analysis.report)
    report["estimates"]["pooled"]["se"] = 1e-6  # within 10% but many SEs away
    units = UnitTruth.join(_rows(analysis), truth)
    assert not _by_name(evaluate_recovery(report, units, ACC.recovery))["pooled_magnitude"].passed


def test_ordering_needs_at_least_one_identifiable_pair(analysis: Analysis, truth: Truth) -> None:
    report = copy.deepcopy(analysis.report)
    for level in ("tier", "industry"):
        for pair in report["hierarchical"]["pairwise"][level]:
            pair["sd_difference"] = 100.0
    units = UnitTruth.join(_rows(analysis), truth)
    result = _by_name(evaluate_recovery(report, units, ACC.recovery))["segment_ordering"]
    assert not result.passed and result.detail["identifiable_pairs"] == 0


def test_interval_level_mismatch_is_refused(analysis: Analysis, truth: Truth) -> None:
    units = UnitTruth.join(_rows(analysis), truth)
    report = copy.deepcopy(analysis.report)
    report["hierarchical"]["sampler"]["interval"] = 0.8
    with pytest.raises(ValueError, match="interval"):
        evaluate_recovery(report, units, ACC.recovery)
    other = ACC.recovery.model_copy(update={"sign_interval": 0.9})
    with pytest.raises(ValueError, match=r"0\.95"):
        evaluate_recovery(analysis.report, units, other)


def test_truth_from_another_world_is_refused(tmp_path: Path, analysis: Analysis) -> None:
    world = _world()
    other = world.with_overrides(n_customers=N + 1)
    with pytest.raises(TruthError, match="different world"):
        Truth.load(_write_truth(tmp_path / "gt.npz", other), world)
    truth = Truth.load(_write_truth(tmp_path / "gt2.npz", world), world)
    rows = _rows(analysis)
    rows[0] = {**rows[0], "tier": "enterprise" if rows[0]["tier"] != "enterprise" else "growth"}
    with pytest.raises(TruthError, match="labels"):
        UnitTruth.join(rows, truth)


def test_contamination_world_checks(contaminated_analysis: Analysis, truth: Truth) -> None:
    units = UnitTruth.join(_rows(contaminated_analysis), truth)
    fractions = {"px-p_up": 0.2, "px-p_down": 0.2}
    results = _by_name(
        evaluate_contamination(contaminated_analysis.report, units, fractions, ACC.contamination)
    )
    assert set(results) == {
        "contamination_detected",
        "iv_recovers_elasticity",
        "itt_attenuated",
        "validity_gates",
    }
    assert all(r.passed for r in results.values()), results
    wrong = {"px-p_up": 0.5, "px-p_down": 0.2}
    assert not _by_name(
        evaluate_contamination(contaminated_analysis.report, units, wrong, ACC.contamination)
    )["contamination_detected"].passed


def test_science_cli(analysis: Analysis, tmp_path: Path) -> None:
    world = _world()
    sim, out = tmp_path / "sim", tmp_path / "analysis"
    sim.mkdir()
    out.mkdir()
    _write_truth(sim / "ground_truth.npz", world)
    (sim / "manifest.json").write_text(json.dumps({"n_customers": N}))
    (out / "report.json").write_text(json.dumps(analysis.report))
    (out / "units.json").write_text(json.dumps(_rows(analysis)))
    scenario = tmp_path / "empty.toml"
    scenario.write_text("")
    args = ["elasticity", "--analysis", str(out), "--sim", str(sim), "--scenario", str(scenario)]
    assert science_main(args) == 0
    assert json.loads((out / "recovery.json").read_text())["passed"] is True
    _write_truth(sim / "ground_truth.npz", world, shift=0.8)  # truth far from the estimates
    assert science_main(args) == 1


@pytest.mark.parametrize("module", ["praxis.science", "praxis.elasticity"])
def test_module_entry_points(module: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m`` entry points parse arguments and exit through SystemExit."""
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", [module, "--help"])
    monkeypatch.delitem(sys.modules, f"{module}.__main__", raising=False)  # fresh execution
    with pytest.raises(SystemExit) as exc:
        runpy.run_module(module, run_name="__main__")
    assert exc.value.code == 0
