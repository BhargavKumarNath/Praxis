"""Pre-registered training protocol (``configs/recovery/acceptance.toml`` [protocol]).

1. *Selection*: fit both models on what was known before ``selection_cutoff``; score the
   first retries of episodes that failed in [selection_cutoff, train_cutoff) and resolved
   before ``train_cutoff``. Champion = lower log loss. Truth-free, out of time.
2. *Artifact*: refit both models and the rate table on everything known before
   ``train_cutoff`` (later outcomes are right-censored by the extract itself).
3. *Diagnostics* on the training data: gap-assignment audit (the identification assumption)
   and the survival model's current-status fit (the parametric-family assumption).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from praxis.recovery import metrics
from praxis.recovery.artifact import finalise_manifest, model_files
from praxis.recovery.classifier import CalibratedClassifier, RateTable, first_retry_rows
from praxis.recovery.config import ModelConfig, RecoveryPolicy
from praxis.recovery.dataset import Episode, build_episodes, failed_between
from praxis.recovery.survival import CureWeibull
from praxis.recovery.warehouse import load_histories

# Survival current-status fit thresholds used for the training diagnostic (reported here;
# the gate applies the pre-registered ones from acceptance.toml in praxis.science).
_DIAG_MIN_ROWS, _DIAG_TOL, _DIAG_Z = 40, 0.05, 3.0


@dataclass(frozen=True)
class Fitted:
    survival: CureWeibull
    classifier: CalibratedClassifier
    table: RateTable


def data_version(episodes: Sequence[Episode]) -> str:
    h = hashlib.sha256()
    for e in episodes:
        lo, hi = e.interval
        row = [e.invoice_id, e.failed_at.isoformat(), lo, str(hi), *astuple(e.features)]
        h.update(json.dumps(row).encode())
    return f"recovery-data-{h.hexdigest()[:16]}"


def fit_all(episodes: Sequence[Episode], cfg: ModelConfig) -> Fitted:
    s = cfg.survival
    return Fitted(
        survival=CureWeibull.fit(episodes, ridge=s.ridge, max_iter=s.max_iter, gtol=s.gtol),
        classifier=CalibratedClassifier(cfg.classifier).fit(episodes),
        table=RateTable().fit(episodes),
    )


def first_retry_scores(fitted: Fitted, episodes: Sequence[Episode]) -> dict[str, Any]:
    """Predictions of every model on the first-retry rows of ``episodes``."""
    kept, elapsed, label = first_retry_rows(episodes)
    rows = [e.features for e in kept]
    preds = {
        CureWeibull.name: fitted.survival.prob_at(rows, elapsed),
        CalibratedClassifier.name: fitted.classifier.predict(rows, elapsed),
        RateTable.name: fitted.table.predict(rows, elapsed),
    }
    return {"episodes": kept, "elapsed": elapsed, "label": label, "predictions": preds}


def assignment_audit(episodes: Sequence[Episode], choices: Sequence[int]) -> dict[str, Any]:
    kept, elapsed, _ = first_retry_rows(episodes)
    gaps = [round(t) for t in elapsed.tolist()]
    reasons = [e.features.reason for e in kept]
    return {
        "rows": len(gaps),
        "counts": {str(c): sum(1 for g in gaps if g == c) for c in choices},
        "uniform_p": metrics.uniform_assignment_p(gaps, choices),
        "independent_of_reason_p": metrics.independence_p(reasons, gaps),
    }


def survival_fit_check(
    survival: CureWeibull, episodes: Sequence[Episode], *, min_rows: int, tolerance: float, z: float
) -> list[dict[str, Any]]:
    kept, elapsed, label = first_retry_rows(episodes)
    if not kept:
        return []
    pred = survival.prob_at([e.features for e in kept], elapsed)
    return metrics.current_status_fit(
        pred,
        label,
        [e.features.reason for e in kept],
        elapsed,
        min_rows=min_rows,
        tolerance=tolerance,
        z=z,
    )


def train(  # noqa: PLR0913 - the protocol's inputs, all explicit
    db: Path,
    *,
    selection_cutoff: datetime,
    train_cutoff: datetime,
    cfg: ModelConfig,
    policy: RecoveryPolicy,
    gap_choices: Sequence[int],
    code_revision: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    if not selection_cutoff < train_cutoff:
        raise ValueError("selection_cutoff must precede train_cutoff")
    selection_eps = build_episodes(load_histories(db, selection_cutoff))
    train_eps = build_episodes(load_histories(db, train_cutoff))
    stage1 = fit_all(selection_eps, cfg)
    validation = failed_between(train_eps, selection_cutoff, train_cutoff)
    scores = first_retry_scores(stage1, validation)
    label = scores["label"]
    selection = {
        name: {
            "rows": len(label),
            "log_loss": metrics.log_loss(p, label),
            "brier": metrics.brier(p, label),
        }
        for name, p in scores["predictions"].items()
    }
    candidates = (CureWeibull.name, CalibratedClassifier.name)
    champion = min(candidates, key=lambda n: (selection[n]["log_loss"], n))
    challenger = next(n for n in candidates if n != champion)

    final = fit_all(train_eps, cfg)
    files = model_files(final.survival, final.classifier, final.table)
    audit = assignment_audit(train_eps, gap_choices)
    fit_cells = survival_fit_check(
        final.survival, train_eps, min_rows=_DIAG_MIN_ROWS, tolerance=_DIAG_TOL, z=_DIAG_Z
    )
    core: dict[str, Any] = {
        "champion": champion,
        "challenger": challenger,
        "created_at": (now or datetime.now(UTC)).isoformat(),
        "code_revision": code_revision,
        "data_version": data_version(train_eps),
        "policy_version": policy.version,
        "model_config": cfg.model_dump(mode="json"),
        "model_config_hash": cfg.config_hash,
        "selection_cutoff": selection_cutoff.isoformat(),
        "train_cutoff": train_cutoff.isoformat(),
        "training": {
            "episodes": len(train_eps),
            "recovered": sum(e.recovered for e in train_eps),
            "first_retry_rows": len(first_retry_rows(train_eps)[0]),
            "selection_episodes": len(selection_eps),
        },
        "selection": selection,
        "survival": {
            "converged": final.survival.converged,
            "iterations": final.survival.iterations,
            "log_likelihood": final.survival.log_likelihood,
        },
    }
    manifest = finalise_manifest(core, files)
    report = {
        "model_version": manifest["model_version"],
        "champion": champion,
        "selection": selection,
        "assignment_audit": audit,
        "survival_fit_cells": fit_cells,
        "survival_fit_cells_failed": sum(not c["passed"] for c in fit_cells),
        "training": core["training"],
    }
    return manifest, files, report
