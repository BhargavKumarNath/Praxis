"""Run the full elasticity analysis on a warehouse extract (truth-free).

Order: build eligible units -> experiment validity -> estimators (pooled, segment, cell,
per-test, dose, IV, naive) -> empirical-Bayes cross-check -> hierarchical Bayesian model.
The report states which gates passed; ``praxis.science`` later compares it with ground truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from praxis.elasticity.bayes import fit_hierarchical
from praxis.elasticity.config import ElasticityConfig, ExperimentDesign
from praxis.elasticity.dataset import UnitTable, build_units
from praxis.elasticity.estimators import (
    CellInput,
    SlopeEstimate,
    empirical_bayes,
    naive_pre_post,
    within_iv,
    within_slopes,
)
from praxis.elasticity.validity import experiment_checks, guardrails
from praxis.elasticity.warehouse import WarehouseExtract

CELL_SEPARATOR = "|"
SYNTHETIC_NOTICE = "SYNTHETIC: simulated customers and prices; not real business results"


@dataclass(frozen=True)
class Analysis:
    report: dict[str, Any]
    units: UnitTable  # every eligible unit (missing outcomes included)
    in_model: NDArray[np.bool_]  # outcome observed and cell estimated (hierarchical sample)

    @property
    def passed(self) -> bool:
        return bool(self.report["validity"]["passed"]) and bool(
            self.report["hierarchical"]["diagnostics"]["passed"]
        )


def cell_label(tier: str, industry: str) -> str:
    return f"{tier}{CELL_SEPARATOR}{industry}"


def _as_dict(estimates: dict[str, SlopeEstimate]) -> dict[str, dict[str, float | int]]:
    return {k: v.to_dict() for k, v in sorted(estimates.items())}


def _slopes(
    units: UnitTable, segment: NDArray[np.generic], cfg: ElasticityConfig, *, min_units: int = 2
) -> dict[str, SlopeEstimate]:
    return within_slopes(
        units.delta,
        units.x_assigned,
        [units.experiment],
        segment,
        units.customer,
        ci_level=cfg.estimation.ci_level,
        min_units=min_units,
    )


def _per_test(
    units: UnitTable, designs: tuple[ExperimentDesign, ...], cfg: ElasticityConfig
) -> dict[str, Any]:
    names = np.array([designs[k].id for k in units.experiment.tolist()])
    itt = _slopes(units, names, cfg)
    out: dict[str, Any] = {}
    for k, d in enumerate(designs):
        m = units.experiment == k
        exposed = m & ~np.isnan(units.x_exposed)
        iv = within_iv(
            units.delta[exposed],
            units.x_exposed[exposed],
            units.x_assigned[exposed],
            [units.experiment[exposed]],
            units.customer[exposed],
            ci_level=cfg.estimation.ci_level,
        )
        treated = units.delta[m & units.assigned_treatment]
        out[d.id] = {
            "product": d.product,
            "log_price_ratio": d.log_ratio,
            "itt": itt[d.id].to_dict() if d.id in itt else None,
            "iv": iv.to_dict() if iv else None,
            "naive_pre_post": (
                naive_pre_post(treated, d.log_ratio, ci_level=cfg.estimation.ci_level).to_dict()
                if treated.size >= 2
                else None
            ),
        }
    return out


def _pooled_iv(units: UnitTable, cfg: ElasticityConfig) -> dict[str, Any] | None:
    exposed = ~np.isnan(units.x_exposed)
    iv = within_iv(
        units.delta[exposed],
        units.x_exposed[exposed],
        units.x_assigned[exposed],
        [units.experiment[exposed]],
        units.customer[exposed],
        ci_level=cfg.estimation.ci_level,
    )
    return iv.to_dict() if iv else None


def _cells(
    units: UnitTable, cfg: ElasticityConfig
) -> tuple[list[CellInput], NDArray[np.float64], NDArray[np.bool_]]:
    labels = np.array([cell_label(t, i) for t, i in zip(units.tier, units.industry, strict=True)])
    est = _slopes(units, labels, cfg, min_units=cfg.estimation.min_units_per_cell)
    cells = []
    for name in sorted(est):
        tier, industry = name.split(CELL_SEPARATOR)
        cells.append(CellInput(name, tier, industry, est[name].estimate, est[name].se))
    weights = np.array([units.design_weight[labels == c.cell].sum() for c in cells])
    in_model = np.isin(labels, [c.cell for c in cells])
    return cells, weights, in_model


def analyse(extract: WarehouseExtract, cfg: ElasticityConfig) -> Analysis:
    units, designs, census = build_units(extract, cfg)
    checks = [
        c.to_dict()
        for k in range(len(designs))
        for c in experiment_checks(units, k, designs, census[k], cfg.validity)
    ]
    validity_passed = all(c["passed"] for c in checks if c["gate"])
    obs = units.subset(~units.missing_outcome)
    sign = np.array(
        ["raise" if designs[k].log_ratio > 0 else "cut" for k in obs.experiment.tolist()]
    )

    cells, weights, cell_mask = _cells(obs, cfg)
    eb_cells, eb_tau = empirical_bayes(cells)
    hier = fit_hierarchical(cells, weights, cfg)
    in_model = np.zeros(units.n, dtype=bool)
    in_model[np.flatnonzero(~units.missing_outcome)[cell_mask]] = True

    report: dict[str, Any] = {
        "notice": SYNTHETIC_NOTICE,
        "data_version": units.data_version(),
        "experiments": [
            {
                "id": d.id,
                "product": d.product,
                "eligible_units": census[k].eligible_units,
                "customers_with_logged_exposure": census[k].logged_customers,
                "guardrails": guardrails(units.subset(units.experiment == k)),
            }
            for k, d in enumerate(designs)
        ],
        "validity": {"passed": validity_passed, "checks": checks},
        "estimates": {
            "pooled": _as_dict(_slopes(obs, np.full(obs.n, "all"), cfg))["all"],
            "pooled_iv": _pooled_iv(obs, cfg),
            "tier": _as_dict(_slopes(obs, obs.tier, cfg)),
            "industry": _as_dict(_slopes(obs, obs.industry, cfg)),
            "cell": {c.cell: {"estimate": c.estimate, "se": c.se} for c in cells},
            "dose": _as_dict(_slopes(obs, sign, cfg)),
            "per_test": _per_test(obs, designs, cfg),
        },
        "empirical_bayes": {
            "tau": eb_tau,
            "cells": {
                p.cell: {"estimate": p.estimate, "sd": p.sd, "shrinkage": p.shrinkage}
                for p in eb_cells
            },
        },
        "hierarchical": hier,
        "cells_included": [c.cell for c in cells],
        "units": {
            "eligible": units.n,
            "outcome_observed": obs.n,
            "in_model": int(in_model.sum()),
            "missing_outcome_rate": float(units.missing_outcome.mean()) if units.n else math.nan,
        },
    }
    return Analysis(report, units, in_model)
