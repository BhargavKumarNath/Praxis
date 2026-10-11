"""Train the churn hazard: rolling-origin backtest -> pre-registered champion rule -> artifact.

Backtest (truth-free, observable labels only): each of the last ``backtest_windows`` snapshot
windows is scored by models fitted on the windows that END before it starts (no overlap, no
future). The GLM is champion only if its summed Poisson deviance beats the tier rate table's;
otherwise the rate table is champion (configs/customer/value.toml). The final models are then
refitted on every window of the training range.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray

from praxis.customer.artifact import finalise_manifest, model_files
from praxis.customer.config import CustomerValueConfig
from praxis.customer.dataset import Panel
from praxis.customer.features import FEATURE_VERSION, FEATURES
from praxis.customer.hazard import (
    HazardModel,
    PoissonHazard,
    RateTable,
    deviance,
    fit_poisson,
    fit_rate_table,
)
from praxis.provenance import code_revision

F64 = NDArray[np.float64]


def fit_glm(panel: Panel, cfg: CustomerValueConfig) -> PoissonHazard:
    h = cfg.hazard
    return fit_poisson(
        panel.x,
        panel.voluntary,
        panel.exposure,
        FEATURES,
        ridge=h.ridge,
        max_iter=h.max_iter,
        tol=h.tol,
    )


def fit_table(panel: Panel, cfg: CustomerValueConfig, *, involuntary: bool = False) -> RateTable:
    events = panel.involuntary if involuntary else panel.voluntary
    name = "involuntary_rate_table" if involuntary else "tier_rate_table"
    return fit_rate_table(
        panel.tiers,
        events,
        panel.exposure,
        prior_weight=cfg.hazard.rate_prior_weight,
        name=name,
    )


def expected(model: HazardModel, panel: Panel) -> F64:
    out: F64 = model.predict(panel.x, panel.tiers) * panel.exposure
    return out


def calibration(panel: Panel, mu: F64, segments: tuple[str, ...]) -> dict[str, Any]:
    """Observed / expected events, overall and per segment."""
    tiers = np.array(panel.tiers)
    out: dict[str, Any] = {
        "observed": float(panel.voluntary.sum()),
        "expected": float(mu.sum()),
        "ratio": _ratio(float(panel.voluntary.sum()), float(mu.sum())),
        "segments": {},
    }
    for s in segments:
        m = tiers == s
        o, e = float(panel.voluntary[m].sum()), float(mu[m].sum())
        out["segments"][s] = {"rows": int(m.sum()), "observed": o, "expected": e}
        out["segments"][s]["ratio"] = _ratio(o, e)
    return out


def _ratio(o: float, e: float) -> float | None:
    return o / e if e > 0 else None


@dataclass(frozen=True)
class Backtest:
    windows: list[dict[str, Any]]
    glm_deviance: float
    table_deviance: float

    @property
    def champion(self) -> str:
        return "poisson_glm" if self.glm_deviance < self.table_deviance else "tier_rate_table"


def backtest(panel: Panel, cfg: CustomerValueConfig) -> Backtest:
    w = cfg.label.window_days
    rows = []
    glm_dev = table_dev = 0.0
    k_last = len(panel.dates)
    for k in range(max(k_last - cfg.hazard.backtest_windows, 1), k_last):
        start = panel.dates[k]
        train_idx = [j for j, d in enumerate(panel.dates) if d + timedelta(days=w) <= start]
        if not train_idx:
            continue
        train = panel.subset(np.isin(panel.window, train_idx))
        test = panel.subset(panel.window == k)
        glm, table = fit_glm(train, cfg), fit_table(train, cfg)
        mu_glm, mu_table = expected(glm, test), expected(table, test)
        dg, dt = deviance(test.voluntary, mu_glm), deviance(test.voluntary, mu_table)
        glm_dev += dg
        table_dev += dt
        rows.append(
            {
                "snapshot": start.isoformat(),
                "train_windows": len(train_idx),
                "rows": test.n,
                "events": float(test.voluntary.sum()),
                "glm_deviance": dg,
                "table_deviance": dt,
                "glm_calibration": calibration(test, mu_glm, cfg.segments.names),
                "table_calibration": calibration(test, mu_table, cfg.segments.names),
            }
        )
    if not rows:
        raise ValueError("the training range holds too few windows for a backtest")
    return Backtest(rows, glm_dev, table_dev)


def _coefficients(glm: PoissonHazard) -> list[dict[str, float | str]]:
    se = np.sqrt(np.diag(glm.cov))
    names = ("intercept", *glm.features)
    return [
        {"name": n, "coef": float(c), "se": float(s)}
        for n, c, s in zip(names, glm.coef, se, strict=True)
    ]


def train(
    panel: Panel, cfg: CustomerValueConfig, *, cutoff: date, now: datetime | None = None
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """(manifest, files, report). Nothing is written here."""
    bt = backtest(panel, cfg)
    glm, table = fit_glm(panel, cfg), fit_table(panel, cfg)
    involuntary = fit_table(panel, cfg, involuntary=True)
    files = model_files(glm, table, involuntary)
    metrics = {
        "backtest_glm_deviance": bt.glm_deviance,
        "backtest_table_deviance": bt.table_deviance,
        "train_rows": panel.n,
        "train_voluntary_events": float(panel.voluntary.sum()),
        "train_involuntary_events": float(panel.involuntary.sum()),
        "train_exposure_days": float(panel.exposure.sum()),
    }
    core = {
        "champion": bt.champion,
        "challenger": "tier_rate_table" if bt.champion == "poisson_glm" else "poisson_glm",
        "data_version": panel.data_version(),
        "feature_version": FEATURE_VERSION,
        "config_hash": cfg.config_hash,
        "code_revision": code_revision(),
        "training_cutoff": cutoff.isoformat(),
        "snapshot_dates": [d.isoformat() for d in panel.dates],
        "label": cfg.label.model_dump(),
        "parameters": cfg.hazard.model_dump(),
        "metrics": metrics,
        "created_at": (now or datetime.now(UTC)).isoformat(),
    }
    manifest = finalise_manifest(core, files)
    report = {
        "is_synthetic": True,
        "model_version": manifest["model_version"],
        "champion": bt.champion,
        "data_version": manifest["data_version"],
        "backtest": bt.windows,
        "metrics": metrics,
        "coefficients": _coefficients(glm),
        "tier_rates": table.state()["rates"],
        "involuntary_rates": involuntary.state()["rates"],
    }
    return manifest, files, report
