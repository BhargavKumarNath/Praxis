"""Customer-level heterogeneity of the price elasticity, from randomised arms (Phase 9).

The Phase 5 estimates are POPULATION means. A pricing decision applies them to one book of
customers whose demand is concentrated in a few heavy users, so the book's demand-weighted
elasticity deviates from the population mean by sqrt(sum_i w_i^2 Var(e_i)) in SD (w_i: the
customer's share of the product's demand). Var(e_i) is not observable per customer, but it is
identified by randomisation: with unit outcome delta_i = e_i x_i + noise_i and noise independent
of the arm, Var(delta | treated) - Var(delta | control) = x^2 Var(e_i) (randomisation-based
heterogeneity, Ding, Feller and Miratrix 2016). Estimated per tier on every test's eligible,
cleanly exposed units, pooled across tests by inverse variance; the SE uses the normal
approximation Var(s^2) = 2 s^4 / (n - 1).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from praxis.elasticity.dataset import EXPOSED_OK, UnitTable

MIN_UNITS = 30  # per arm, per (test, tier)


def heterogeneity(units: UnitTable, log_ratios: list[float]) -> dict[str, dict[str, Any]]:
    """Per tier: {"variance", "se", "tests"} of the customer elasticity (variance >= 0)."""
    ok = (~units.missing_outcome) & (units.exposure_status == EXPOSED_OK)
    out: dict[str, dict[str, Any]] = {}
    for tier in sorted(set(units.tier.tolist())):
        ests, ses = [], []
        for k, x in enumerate(log_ratios):
            sel = ok & (units.tier == tier) & (units.experiment == k)
            t = units.delta[sel & units.assigned_treatment]
            c = units.delta[sel & ~units.assigned_treatment]
            if len(t) < MIN_UNITS or len(c) < MIN_UNITS:
                continue
            vt, vc = float(np.var(t, ddof=1)), float(np.var(c, ddof=1))
            x2 = x * x
            ests.append((vt - vc) / x2)
            ses.append(math.sqrt(2 * vt**2 / (len(t) - 1) + 2 * vc**2 / (len(c) - 1)) / x2)
        if not ests:
            continue
        w = [1.0 / s**2 for s in ses]
        pooled = sum(wi * e for wi, e in zip(w, ests, strict=True)) / sum(w)
        out[tier] = {
            "variance": max(pooled, 0.0),
            "raw": pooled,
            "se": math.sqrt(1.0 / sum(w)),
            "tests": len(ests),
        }
    return out
