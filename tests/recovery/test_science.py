"""Science evaluation: closed-loop replay against truth, oracle, bootstrap, full report, CLI."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from praxis.domain.dunning import DunningState
from praxis.recovery.artifact import save_artifact
from praxis.recovery.config import load_model_config, load_policy
from praxis.recovery.policy import DecisionContext, RecoveryDecider, RecoveryDecision
from praxis.recovery.train import train
from praxis.science import __main__ as science_cli
from praxis.science.recovery import (
    EvaluationError,
    Inputs,
    collect_truth,
    ece_null_quantile,
    evaluate,
    load_acceptance,
    oracle,
    paired_bootstrap,
    replay,
    summarise,
)
from praxis.simulator.config import load_config
from praxis.simulator.engine import RecoveryTruth
from tests.recovery.helpers import episodes
from tests.recovery.world import SCENARIO, World

POLICY = load_policy()
START = datetime(2026, 1, 5, tzinfo=UTC)


def truth(cured: bool, cure_days: float) -> RecoveryTruth:
    return RecoveryTruth(
        "inv", "cust", 0, "card_declined", 0.5, 1.0, 3.0, cured, cure_days if cured else math.inf
    )


def schedule_decider(days: tuple[float, ...]) -> Callable[[DecisionContext], Any]:
    def decide(ctx: DecisionContext) -> RecoveryDecision:
        k = ctx.attempts_made - 1
        retry = k < len(days)
        return RecoveryDecision(
            "retry" if retry else "stop",
            days[k] if retry else None,
            DunningState.GRACE if retry else DunningState.SUSPENDED,
            RecoveryDecider(POLICY, None).baseline(ctx).policy_kind,
            POLICY.version,
            None,
            None,
            None,
            None,
        )

    return decide


def test_replay_settles_retries_with_the_true_cure_time() -> None:
    ep = episodes(1, seed=0)[0]
    a = ep.amount_minor
    hit = replay(ep, truth(True, 4.5), schedule_decider((3.0, 10.0)), POLICY)
    assert (hit.recovered, hit.recovery_day, hit.retries, hit.failed_retries) == (True, 10.0, 2, 1)
    assert hit.net_value == pytest.approx(a * (1 - 0.004 * 10) - 2 * 30 - 150)
    assert hit.revenue == a and not hit.bound_violation
    miss = replay(ep, truth(False, 0.0), schedule_decider((3.0, 10.0)), POLICY)
    assert (miss.recovered, miss.failed_retries, miss.net_value) == (False, 2, -2 * 30 - 2 * 150)
    assert miss.suspended_days == pytest.approx(POLICY.bounds.horizon_days - 10)
    exact = replay(ep, truth(True, 3.0), schedule_decider((3.0,)), POLICY)
    assert exact.recovered and exact.recovery_day == 3.0  # e >= C succeeds
    bad = replay(ep, truth(False, 0.0), schedule_decider((5.0, 4.0, 25.0)), POLICY)
    assert bad.bound_violation


def test_oracle_and_summary() -> None:
    ep = episodes(1, seed=0)[0]
    assert oracle(ep, truth(False, 0.0), POLICY) == 0.0
    assert oracle(ep, truth(True, 25.0), POLICY) == 0.0
    assert oracle(ep, truth(True, 2.2), POLICY) == pytest.approx(
        ep.amount_minor * (1 - 0.004 * 3) - 30
    )
    s = summarise([replay(ep, truth(True, 1.0), schedule_decider((1.0,)), POLICY)])
    assert s["recovery_rate"] == 1.0 and s["failed_retries"] == 0
    assert math.isnan(summarise([])["recovery_rate"])


def test_bootstrap_and_ece_noise_floor() -> None:
    mean, lo, hi = paired_bootstrap(np.r_[np.ones(50), np.zeros(50)], 500, 0)
    assert mean == 0.5 and lo < 0.5 < hi
    p = np.random.default_rng(0).uniform(0.05, 0.6, 500)
    assert 0.02 < ece_null_quantile(p, 300, 0) < 0.09


def test_world_without_recovery_truth_is_refused() -> None:
    with pytest.raises(EvaluationError):
        collect_truth(load_config().with_overrides(n_customers=10, days=5), 1)


@pytest.fixture(scope="module")
def models(world: World, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("rc-models")
    manifest, files, _ = train(
        world.db,
        selection_cutoff=START + timedelta(days=100),
        train_cutoff=START + timedelta(days=120),
        cfg=load_model_config(),
        policy=POLICY,
        gap_choices=(1, 2, 3, 4, 5, 7, 10),
        code_revision="test",
    )
    save_artifact(manifest, files, root)
    return root


@pytest.mark.slow
def test_full_evaluation_report(world: World, models: Path) -> None:
    report = evaluate(Inputs(world.config, world.seed, world.db, models, POLICY, load_acceptance()))
    names = {c["name"] for c in report["checks"]}
    assert names == {
        "champion_ece_consistent_with_calibration",
        "champion_segment_calibration",
        "champion_brier_vs_table",
        "champion_pr_auc_vs_table",
        "champion_brier_vs_base_rate",
        "gap_assignment_uniform",
        "gap_assignment_independent_of_reason",
        "survival_current_status_fit",
        "champion_truth_recovery",
        "policy_net_value_gain",
        "policy_recovery_rate",
        "policy_recovered_revenue",
        "fallback_equals_baseline",
        "policy_bounds",
    }
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["fallback_equals_baseline"]["passed"]
    assert checks["policy_bounds"]["passed"] and checks["gap_assignment_uniform"]["passed"]
    pol = report["policy"]
    assert (
        pol["fallback_model_unavailable"]["net_value_minor"] == pol["baseline"]["net_value_minor"]
    )
    assert report["evaluation_episodes"] == pol["baseline"]["episodes"] > 0
    assert pol["oracle_net_value_minor"] >= max(
        pol[n]["net_value_minor"] for n in ("baseline", "champion", "challenger")
    )
    assert report["is_synthetic"] and report["passed"] == all(c["passed"] for c in report["checks"])


@pytest.mark.slow
def test_cli_and_refusals(world: World, models: Path, tmp_path: Path) -> None:
    out = tmp_path / "eval"
    args = [
        "recovery",
        "--db",
        str(world.db),
        "--sim",
        str(world.sim_dir),
        "--scenario",
        str(SCENARIO),
        "--models",
        str(models),
        "--out",
        str(out),
    ]
    assert science_cli.main(args) in (0, 1)
    assert json.loads((out / "evaluation.json").read_text())["seed"] == world.seed
    empty = tmp_path / "none"
    empty.mkdir()
    assert science_cli.main([*args[:8], str(empty), "--out", str(out)]) == 1
    acc = tmp_path / "acc.toml"
    acc.write_text(
        (Path(__file__).resolve().parents[2] / "configs/recovery/acceptance.toml")
        .read_text()
        .replace("train_cutoff_day = 120", "train_cutoff_day = 121")
    )
    assert science_cli.main([*args, "--acceptance", str(acc)]) == 1
    other = tmp_path / "pol.toml"
    other.write_text(
        (Path(__file__).resolve().parents[2] / "configs/recovery/policy.toml")
        .read_text()
        .replace("retry_cost_minor = 30", "retry_cost_minor = 31")
    )
    assert science_cli.main([*args, "--policy", str(other)]) == 1


@pytest.mark.slow
def test_known_failures_only_mask_listed_checks_on_their_own_world(
    world: World, models: Path, tmp_path: Path
) -> None:
    out = tmp_path / "eval"
    base = [
        "recovery",
        "--db",
        str(world.db),
        "--sim",
        str(world.sim_dir),
        "--scenario",
        str(SCENARIO),
        "--models",
        str(models),
        "--out",
        str(out),
    ]
    science_cli.main(base)
    report = json.loads((out / "evaluation.json").read_text())
    failing = [c["name"] for c in report["checks"] if not c["passed"]]
    known = tmp_path / "known.json"
    known.write_text(json.dumps({"seed": world.seed, "checks": failing, "reason": "test"}))
    assert science_cli.main([*base, "--known-failures", str(known)]) == 0
    assert json.loads((out / "evaluation.json").read_text())["passed"] == (not failing)
    known.write_text(json.dumps({"seed": world.seed, "checks": [], "reason": "test"}))
    assert science_cli.main([*base, "--known-failures", str(known)]) == (1 if failing else 0)
    known.write_text(json.dumps({"seed": 42, "checks": failing, "reason": "test"}))
    with pytest.raises(SystemExit):
        science_cli.main([*base, "--known-failures", str(known)])
