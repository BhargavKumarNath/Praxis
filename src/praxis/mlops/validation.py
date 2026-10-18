"""Offline validation gate for a demand-forecast candidate (CANDIDATE -> CHALLENGER / REJECTED).

Checks (configs/mlops/demand.toml ``[validation]``; thresholds pre-registered):

* ``artifact_integrity``: the artifact loads (format, feature version, every file checksum).
* ``lineage_complete``: the registered record is complete and matches the manifest.
* ``feature_contract``: feature version = serving code; catalogue / horizons / quantiles
  identical to the champion's (else the serving router could not swap it in).
* ``dataset_reference``: the warehouse reproduces the manifest's ``data_version`` for the
  training window; enough days and rows.
* ``leakage_training_window``: no training target on or after the decision date.
* ``leakage_future_invariance``: multiplying every day after an origin by a poison factor
  leaves the candidate's forecasts at that origin unchanged.
* ``backtest_acceptance``: the Phase 4 pre-registered rolling-origin acceptance passed on
  the candidate's own training panel (report matched by data version and config hash).
* ``baseline_comparison``, ``calibration_80``, ``calibration_50``,
  ``segment_regression_vs_baseline``: the named parts of that acceptance, surfaced.
* ``champion_non_inferiority``: the candidate's backtest skill (vWAPE / seasonal naive) is
  at most ``max_skill_ratio_vs_champion`` x the champion's (amendment 2026-10-17: the
  development world showed a misconfigured recipe clearing every absolute check).
* ``latency_budget``: p95 of a full-catalogue prediction <= serving budget.
* ``memory_budget``: Python heap peak while loading + predicting; artifact size on disk.
* ``reproducibility``: retraining on the same data / config / code revision reproduces the
  artifact checksum exactly.
* ``serving_parity``: the LOADED artifact predicts exactly like the freshly trained model
  (added after the ridge loader defect of 2026-10-11, which no other check could see:
  the backtest scores in-memory models and the checksum covers files, not behaviour).

Integrity failure stops the run (nothing else can be checked on a corrupt artifact); every
other check always runs, so a report lists every reason a candidate failed.
"""

from __future__ import annotations

import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from praxis.forecasting.artifact import (
    ArtifactError,
    ForecastArtifact,
    load_artifact,
    train_artifact_with_model,
)
from praxis.forecasting.backtest import NAIVE, evaluate_acceptance
from praxis.forecasting.config import Acceptance
from praxis.forecasting.features import FEATURE_VERSION, WINDOW, build_features, build_frame
from praxis.forecasting.panel import DemandPanel
from praxis.mlops.config import MlopsConfig
from praxis.mlops.lineage import missing_lineage
from praxis.mlops.scoring import (
    IncompatibleVersions,
    WarehouseData,
    check_compatible,
    load_data,
)
from praxis.mlops.store import VersionRecord

Predictor = Callable[[DemandPanel, int], np.ndarray[Any, Any]]


@dataclass(frozen=True)
class Gate:
    """What a candidate is judged against: the lifecycle policy and the AUTHORITATIVE
    forecast acceptance (the pinned configs/forecast/demand.toml), never the candidate's."""

    policy: MlopsConfig
    acceptance: Acceptance


def check(
    name: str, passed: bool, value: Any = None, threshold: str = "", **detail: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {"check": name, "passed": bool(passed), "threshold": threshold}
    out["value"] = round(value, 6) if isinstance(value, float) else value
    if detail:
        out["detail"] = detail
    return out


def report(model_version: str, checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "model_version": model_version,
        "is_synthetic": True,
        "checks": checks,
        "failed": [c["check"] for c in checks if not c["passed"]],
        "passed": all(c["passed"] for c in checks),
        **extra,
    }


# ------------------------------------------------------------------ individual checks
def check_lineage(record: VersionRecord, art: ForecastArtifact, cfg: MlopsConfig) -> dict[str, Any]:
    problems = missing_lineage(record)
    m = art.manifest
    if record.artifact_checksum != m["artifact_checksum"]:
        problems.append("artifact_checksum != manifest")
    if record.data_version != m["data_version"]:
        problems.append("data_version != manifest")
    if record.model_version != art.model_version:
        problems.append("model_version != manifest")
    if cfg.validation.require_clean_code_revision and str(record.code_revision).endswith("-dirty"):
        problems.append("code_revision is dirty")
    return check("lineage_complete", not problems, len(problems), "0 problems", problems=problems)


def check_feature_contract(
    art: ForecastArtifact, champion: ForecastArtifact | None
) -> dict[str, Any]:
    problems = []
    if art.manifest["feature_version"] != FEATURE_VERSION:
        problems.append(f"feature_version {art.manifest['feature_version']} != {FEATURE_VERSION}")
    if champion is not None:
        try:
            check_compatible([champion, art])
        except IncompatibleVersions as exc:
            problems.append(str(exc))
    return check(
        "feature_contract",
        not problems,
        FEATURE_VERSION,
        "same feature contract as serving code and champion",
        problems=problems,
        champion=None if champion is None else champion.model_version,
    )


def training_data(art: ForecastArtifact, warehouse: Path) -> WarehouseData:
    t = art.manifest["training"]
    return load_data(
        warehouse,
        start=date.fromisoformat(t["panel_start"]),
        end=date.fromisoformat(t["panel_end"]),
        series=art.series,
    )


def check_dataset(art: ForecastArtifact, data: WarehouseData, cfg: MlopsConfig) -> dict[str, Any]:
    v = cfg.validation
    rows = int(art.manifest["training"]["rows"])
    reproduced = data.panel.data_version()
    problems = []
    if reproduced != art.manifest["data_version"]:
        problems.append(f"warehouse data_version {reproduced} != {art.manifest['data_version']}")
    if data.panel.n_days < v.min_training_days:
        problems.append(f"{data.panel.n_days} training days < {v.min_training_days}")
    if rows < v.min_training_rows:
        problems.append(f"{rows} training rows < {v.min_training_rows}")
    return check(
        "dataset_reference",
        not problems,
        reproduced,
        f"= manifest data_version, >= {v.min_training_days} days, >= {v.min_training_rows} rows",
        problems=problems,
        days=data.panel.n_days,
        rows=rows,
    )


def check_training_window(art: ForecastArtifact, as_of: date) -> dict[str, Any]:
    last = date.fromisoformat(str(art.manifest["training"]["last_target_date"]))
    return check(
        "leakage_training_window",
        last < as_of,
        last.isoformat(),
        f"< decision date {as_of.isoformat()}",
    )


def artifact_predictor(art: ForecastArtifact, data: WarehouseData) -> Predictor:
    def run(panel: DemandPanel, origin: int) -> np.ndarray[Any, Any]:
        frame = build_features(
            panel,
            origin,
            art.config.target.horizons,
            data.plan,
            art.catalogue,
            external=art.config.features.external,
        )
        out: np.ndarray[Any, Any] = art.model.predict(frame).quantiles
        return out

    return run


def poisoned(panel: DemandPanel, origin: int, factor: float) -> DemandPanel:
    """Every outcome after ``origin`` multiplied by ``factor`` (a corrupted future)."""
    cut = origin + 1

    def scale(arr: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        out = arr.copy()
        out[..., cut:] *= factor
        return out

    return replace(
        panel,
        demand=scale(panel.demand),
        served=scale(panel.served),
        active=scale(panel.active),
        context={k: scale(v) for k, v in panel.context.items()},
    )


def check_future_invariance(
    predictor: Predictor, panel: DemandPanel, factor: float, max_horizon: int
) -> dict[str, Any]:
    origin = panel.n_days - 1 - max_horizon
    clean = predictor(panel, origin)
    dirty = predictor(poisoned(panel, origin, factor), origin)
    diff = float(np.max(np.abs(clean - dirty))) if clean.size else 0.0
    return check(
        "leakage_future_invariance",
        diff == 0.0,
        diff,
        "max |change| = 0 when the future is poisoned",
        origin=panel.date_of(origin).isoformat(),
        factor=factor,
    )


def backtest_checks(
    art: ForecastArtifact,
    backtest: dict[str, Any] | None,
    cfg: MlopsConfig,
    gate: Acceptance,
) -> list[dict[str, Any]]:
    """Re-evaluate the backtest METRICS against the authoritative acceptance (``gate``):
    never trust the report's own verdict, which used the candidate's own config."""
    required = cfg.validation.require_backtest_acceptance
    if backtest is None:
        return [check("backtest_acceptance", not required, None, "report required")]
    problems = []
    if backtest.get("data_version") != art.manifest["data_version"]:
        problems.append("backtest data_version != artifact data_version")
    if backtest.get("config_hash") != art.manifest["config_hash"]:
        problems.append("backtest config_hash != artifact config_hash")
    acceptance = evaluate_acceptance(backtest, art.config.model_copy(update={"acceptance": gate}))
    passed = acceptance["passed"] and not problems
    out = [
        check(
            "backtest_acceptance",
            passed or not required,
            bool(acceptance["passed"]),
            "pre-registered forecast acceptance (configs/forecast/demand.toml) passed",
            problems=problems,
            failed=[c["check"] for c in acceptance["checks"] if not c["passed"]],
        )
    ]
    acc = gate
    cand = backtest["models"][acc.candidate]["overall"]
    naive = backtest["models"][NAIVE]["overall"]
    ratio = cand["vwape"] / naive["vwape"]
    out.append(
        check(
            "baseline_comparison",
            ratio <= acc.max_vwape_ratio_vs_seasonal_naive,
            ratio,
            f"vWAPE / seasonal naive <= {acc.max_vwape_ratio_vs_seasonal_naive}",
        )
    )
    for key, (lo, hi) in (("80", acc.coverage_80), ("50", acc.coverage_50)):
        v = float(cand[f"coverage_{key}"])
        out.append(check(f"calibration_{key}", lo <= v <= hi, v, f"in [{lo}, {hi}]"))
    slices = [c for c in acceptance["checks"] if c["check"].startswith("worst_slice")]
    worst: dict[str, Any] = (
        slices[0] if slices else {"value": None, "passed": False, "check": "missing"}
    )
    out.append(
        check(
            "segment_regression_vs_baseline",
            worst["passed"],
            worst["value"],
            f"<= {acc.max_slice_vwape_ratio_vs_seasonal_naive}",
            slice=worst["check"],
        )
    )
    return out


def backtest_skill(overall: dict[str, Any], name: str) -> float:
    """vWAPE of ``name`` relative to the seasonal naive on the same rolling-origin backtest."""
    return float(overall[name]["vwape"]) / float(overall[NAIVE]["vwape"])


def check_champion_skill(
    backtest: dict[str, Any] | None,
    champion: ForecastArtifact | None,
    cfg: MlopsConfig,
    gate: Acceptance,
) -> dict[str, Any]:
    """Non-inferiority to the champion offline (amendment 2026-10-17, ADR 0017): absolute
    acceptance alone let a clearly worse recipe through to shadow."""
    limit = cfg.validation.max_skill_ratio_vs_champion
    threshold = f"candidate skill / champion skill <= {limit}"
    reference = None if champion is None else champion.manifest.get("backtest")
    if champion is None or reference is None:
        return check("champion_non_inferiority", True, None, threshold, note="no champion backtest")
    if backtest is None:
        required = cfg.validation.require_backtest_acceptance
        return check("champion_non_inferiority", not required, None, threshold)
    name = gate.candidate
    mine = backtest_skill({k: m["overall"] for k, m in backtest["models"].items()}, name)
    theirs = backtest_skill(reference["overall"], name)
    return check(
        "champion_non_inferiority",
        mine / theirs <= limit,
        mine / theirs,
        threshold,
        candidate_skill=round(mine, 6),
        champion_skill=round(theirs, 6),
        champion=champion.model_version,
    )


def check_latency(art: ForecastArtifact, data: WarehouseData, cfg: MlopsConfig) -> dict[str, Any]:
    predictor = artifact_predictor(art, data)
    origin = data.panel.n_days - 1
    timings = []
    for _ in range(cfg.validation.latency_repeats):
        t0 = time.perf_counter()
        predictor(data.panel, origin)
        timings.append((time.perf_counter() - t0) * 1000.0)
    p95 = float(np.quantile(timings, 0.95))
    budget = cfg.validation.latency_budget_ms_p95
    return check(
        "latency_budget",
        p95 <= budget,
        p95,
        f"p95 <= {budget} ms",
        rows=len(art.series) * len(art.config.target.horizons),
    )


def check_memory(path: Path, data: WarehouseData, cfg: MlopsConfig) -> dict[str, Any]:
    size_mb = sum(f.stat().st_size for f in path.iterdir() if f.is_file()) / 2**20
    tracemalloc.start()
    try:
        art = load_artifact(path)
        artifact_predictor(art, data)(data.panel, data.panel.n_days - 1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    peak_mb = peak / 2**20
    v = cfg.validation
    passed = peak_mb <= v.memory_budget_mb and size_mb <= v.max_artifact_mb
    return check(
        "memory_budget",
        passed,
        round(peak_mb, 3),
        f"heap peak <= {v.memory_budget_mb} MB, artifact <= {v.max_artifact_mb} MB",
        artifact_mb=round(size_mb, 3),
    )


def check_reproducibility(
    art: ForecastArtifact, data: WarehouseData, backtest: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Retrain once: the checksum must match, and the LOADED artifact must predict exactly
    like the freshly trained in-memory model (training / serving parity)."""
    m = art.manifest
    manifest, _, model = train_artifact_with_model(
        data.panel, data.plan, art.config, code_revision=str(m["code_revision"]), backtest=backtest
    )
    end = data.panel.n_days - 1
    frame = build_frame(
        data.panel,
        range(max(end - 13, WINDOW - 1), end + 1),
        art.config.target.horizons,
        data.plan,
        art.catalogue,
        external=art.config.features.external,
    )
    trained, served = model.predict(frame), art.model.predict(frame)
    gap = float(
        max(
            np.max(np.abs(trained.point - served.point)),
            np.max(np.abs(trained.quantiles - served.quantiles)),
        )
    )
    return [
        check(
            "reproducibility",
            manifest["artifact_checksum"] == m["artifact_checksum"],
            manifest["artifact_checksum"][:12],
            f"retrained checksum = {str(m['artifact_checksum'])[:12]}",
        ),
        check(
            "serving_parity",
            gap == 0.0,
            gap,
            "loaded artifact predicts exactly like the trained model",
            rows=len(frame),
        ),
    ]


# ----------------------------------------------------------------------- the gate
def validate_candidate(
    path: Path,
    record: VersionRecord,
    *,
    warehouse: Path,
    as_of: date,
    gate: Gate,
    backtest: dict[str, Any] | None,
    champion: ForecastArtifact | None,
) -> dict[str, Any]:
    cfg = gate.policy
    try:
        art = load_artifact(path)
    except (ArtifactError, OSError, KeyError, ValueError) as exc:
        return report(
            record.model_version,
            [
                check(
                    "artifact_integrity", False, None, "loads with valid checksums", error=str(exc)
                )
            ],
            as_of=as_of.isoformat(),
        )
    data = training_data(art, warehouse)
    checks = [
        check("artifact_integrity", True, art.model_version, "loads with valid checksums"),
        check_lineage(record, art, cfg),
        check_feature_contract(art, champion),
        check_dataset(art, data, cfg),
        check_training_window(art, as_of),
        check_future_invariance(
            artifact_predictor(art, data),
            data.panel,
            cfg.validation.leakage_poison_factor,
            max(art.config.target.horizons),
        ),
        *backtest_checks(art, backtest, cfg, gate.acceptance),
        check_champion_skill(backtest, champion, cfg, gate.acceptance),
        check_latency(art, data, cfg),
        check_memory(path, data, cfg),
        *check_reproducibility(art, data, backtest),
    ]
    return report(
        record.model_version,
        checks,
        as_of=as_of.isoformat(),
        champion=None if champion is None else champion.model_version,
        policy_version=cfg.policy_version,
    )
