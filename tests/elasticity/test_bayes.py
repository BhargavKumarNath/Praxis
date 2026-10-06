"""Hierarchical Bayesian model: recovery, diagnostics, PPC, prior sensitivity, reproducibility.

required_test.md s11 "Bayesian diagnostics": convergence, effective sample size, posterior
predictive checks, divergent transitions, prior sensitivity; never accept a model merely
because sampling completed.
"""

from __future__ import annotations

import numpy as np
import pytest

from praxis.elasticity.analysis import Analysis
from praxis.elasticity.bayes import _gates, fit_hierarchical, posterior_predictive, summarise
from praxis.elasticity.config import ElasticityConfig
from praxis.elasticity.estimators import CellInput
from tests.elasticity.helpers import INDUSTRIES, TIERS, true_elasticity


def test_hierarchical_cells_recover_the_known_elasticities(analysis: Analysis) -> None:
    cells = analysis.report["hierarchical"]["cells"]
    assert len(cells) == len(TIERS) * len(INDUSTRIES)
    covered = 0
    for name, s in cells.items():
        tier, industry = name.split("|")
        truth = true_elasticity(tier, industry)
        assert s["mean"] == pytest.approx(truth, abs=0.25)
        covered += s["ci95_low"] <= truth <= s["ci95_high"]
    assert covered >= len(cells) - 1


def test_tier_summaries_and_pairwise_ordering(analysis: Analysis) -> None:
    hier = analysis.report["hierarchical"]
    assert set(hier["tier"]) == set(TIERS)
    assert hier["tier"]["starter"]["mean"] < hier["tier"]["growth"]["mean"]
    assert hier["tier"]["growth"]["mean"] < hier["tier"]["enterprise"]["mean"]
    pairs = {(p["a"], p["b"]): p for p in hier["pairwise"]["tier"]}
    assert pairs[("enterprise", "starter")]["p_a_less_than_b"] < 0.01
    assert all(s["p_negative"] > 0.99 for s in hier["tier"].values())
    assert hier["pooled"]["interval_low"] < hier["pooled"]["mean"] < hier["pooled"]["interval_high"]


def test_every_diagnostic_is_computed_and_gated(analysis: Analysis) -> None:
    diag = analysis.report["hierarchical"]["diagnostics"]
    assert set(diag["gates"]) == {
        "rhat",
        "ess_bulk",
        "ess_tail",
        "divergences",
        "posterior_predictive",
        "prior_sensitivity",
    }
    assert diag["passed"] == all(diag["gates"].values())
    conv = diag["convergence"]
    assert conv["rhat_max"] < 1.01 and conv["divergences"] == 0, conv
    assert set(diag["posterior_predictive_p"]) == {
        "chi2_discrepancy",
        "sd_across_cells",
        "min_cell",
        "max_cell",
    }
    assert set(diag["prior_sensitivity"]["fits"]) == {"wide", "narrow"}
    shifts = diag["prior_sensitivity"]["fits"]["wide"]["shift_in_posterior_sd"]
    assert set(shifts) == {"pooled", "tier:enterprise", "tier:growth", "tier:starter"}


def _cells(analysis: Analysis) -> tuple[list[CellInput], np.ndarray]:
    est = analysis.report["estimates"]["cell"]
    cells = []
    for k, v in sorted(est.items()):
        tier, industry = k.split("|")
        cells.append(CellInput(k, tier, industry, v["estimate"], v["se"]))
    return cells, np.ones(len(cells))


def test_too_small_a_sampler_budget_fails_the_gate(
    analysis: Analysis, cfg: ElasticityConfig
) -> None:
    """Negative control: sampling 'completes', but the ESS gate refuses the fit."""
    cells, w = _cells(analysis)
    tiny = cfg.with_sampler(draws=100, tune=100)
    tiny = tiny.model_copy(
        update={"hierarchical": tiny.hierarchical.model_copy(update={"chains": 2})}
    )
    diag = fit_hierarchical(cells, w, tiny)["diagnostics"]
    assert not diag["gates"]["ess_bulk"] and not diag["passed"]


def test_fit_is_reproducible_for_a_fixed_seed(analysis: Analysis, cfg: ElasticityConfig) -> None:
    cells, w = _cells(analysis)
    small = cfg.with_sampler(draws=200, tune=200)
    a, b = fit_hierarchical(cells, w, small), fit_hierarchical(cells, w, small)
    assert a["cells"] == b["cells"] and a["diagnostics"] == b["diagnostics"]


def test_posterior_predictive_detects_a_misfit() -> None:
    cells = [CellInput(f"c{i}", "t", "i", 5.0, 0.1) for i in range(6)]
    theta = np.zeros((500, 6))
    ppc = posterior_predictive(theta, cells, seed=0)
    assert ppc["chi2_discrepancy"] == 0.0 and ppc["min_cell"] == 0.0


def test_gate_logic(cfg: ElasticityConfig) -> None:
    d = cfg.diagnostics
    good = {"rhat_max": 1.001, "ess_bulk_min": 900.0, "ess_tail_min": 900.0, "divergences": 0.0}
    ppc = {"chi2": 0.5}
    assert all(_gates(good, ppc, 0.1, d).values())
    assert not _gates({**good, "rhat_max": 1.05}, ppc, 0.1, d)["rhat"]
    assert not _gates({**good, "divergences": 1.0}, ppc, 0.1, d)["divergences"]
    assert not _gates({**good, "ess_tail_min": 10.0}, ppc, 0.1, d)["ess_tail"]
    assert not _gates(good, {"chi2": 0.001}, 0.1, d)["posterior_predictive"]
    assert not _gates(good, ppc, 0.9, d)["prior_sensitivity"]
    assert not _gates({**good, "rhat_max": float("nan")}, ppc, 0.1, d)["rhat"]


def test_summarise_quantiles() -> None:
    draws = np.random.default_rng(0).normal(-1.0, 0.1, 100_000)
    s = summarise(draws, 0.90)
    assert s["mean"] == pytest.approx(-1.0, abs=0.002)
    assert s["interval_low"] == pytest.approx(-1.0 - 1.645 * 0.1, abs=0.003)
    assert s["ci95_high"] == pytest.approx(-1.0 + 1.96 * 0.1, abs=0.003)
    assert s["p_negative"] == 1.0
