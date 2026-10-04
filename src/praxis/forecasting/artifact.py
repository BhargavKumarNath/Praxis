"""Train, save and load the demand-forecast artifact.

An artifact is a directory ``<root>/<model_version>/`` with one LightGBM text model per
output and ``manifest.json`` holding the full lineage (CLAUDE.md s10). The model version is
derived from content (model files + lineage, never the timestamp), so identical inputs
give an identical version. Loading verifies every checksum and the feature version.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from praxis.forecasting.backtest import make_dataset
from praxis.forecasting.config import ForecastConfig
from praxis.forecasting.features import FEATURE_VERSION, Catalogue
from praxis.forecasting.models import (
    HybridForecaster,
    LightGBMForecaster,
    ResidualQuantiles,
    RidgeBaseline,
    SeasonalMovingAverage,
)
from praxis.forecasting.panel import DemandPanel, PricePlan, SeriesKey

ARTIFACT_FORMAT = 2  # 2 = hybrid champion (ADR 0011)
MODEL_NAME = "demand_forecaster"
MANIFEST = "manifest.json"
RIDGE_FILE = "ridge.json"
# Excluded from the content hash: the timestamp and the hash fields themselves.
_VOLATILE = ("created_at", "artifact_checksum", "model_version")


class ArtifactError(RuntimeError):
    """Artifact is missing, corrupted or incompatible with this code."""


@dataclass(frozen=True)
class ForecastArtifact:
    manifest: dict[str, Any]
    model: HybridForecaster
    fallback: SeasonalMovingAverage
    catalogue: Catalogue
    config: ForecastConfig
    series: tuple[SeriesKey, ...]

    @property
    def model_version(self) -> str:
        return str(self.manifest["model_version"])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_checksum(manifest: dict[str, Any]) -> str:
    core = {k: v for k, v in manifest.items() if k not in _VOLATILE}
    return _sha(json.dumps(core, sort_keys=True, separators=(",", ":")).encode())


def train_artifact(
    panel: DemandPanel,
    plan: PricePlan,
    cfg: ForecastConfig,
    *,
    code_revision: str,
    backtest: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Fit on every row whose target is observed. Returns (manifest, model files)."""
    ds = make_dataset(panel, plan, cfg)
    rows = np.flatnonzero(~np.isnan(ds.target))
    frame = ds.frame.take(rows)
    model = HybridForecaster.build(cfg.lightgbm, cfg.target.quantiles, cfg.ridge.alpha)
    model.fit(frame, ds.target[rows], ds.train_weight[rows])
    fallback = SeasonalMovingAverage(cfg.target.quantiles)
    fallback.fit(frame, ds.target[rows], ds.train_weight[rows])
    files = {f"{name}.txt": text for name, text in model.quantiles.model_strings().items()}
    files[RIDGE_FILE] = json.dumps(model.ridge.state(), sort_keys=True, separators=(",", ":"))
    catalogue = Catalogue.from_series(panel.series)
    manifest: dict[str, Any] = {
        "artifact_format": ARTIFACT_FORMAT,
        "model_name": MODEL_NAME,
        "champion": {
            "kind": HybridForecaster.name,
            "point": RidgeBaseline.name,
            "quantiles": f"{LightGBMForecaster.name}_calibrated",
            "adr": "0011",
        },
        "is_synthetic": True,
        "created_at": (now or datetime.now(UTC)).isoformat(),
        "code_revision": code_revision,
        "data_version": panel.data_version(),
        "price_plan_version": plan.version(),
        "feature_version": FEATURE_VERSION,
        "feature_names": list(frame.names),
        "config": cfg.model_dump(mode="json"),
        "config_hash": cfg.config_hash,
        "quantiles": list(cfg.target.quantiles),
        "horizons": list(cfg.target.horizons),
        "catalogue": {
            "regions": list(catalogue.regions),
            "products": list(catalogue.products),
            "segments": list(catalogue.segments),
        },
        "series": [[s.region_id, s.product, s.segment] for s in panel.series],
        "training": {
            "rows": len(frame),
            "panel_start": panel.start_date.isoformat(),
            "panel_end": panel.end_date.isoformat(),
            "last_target_date": panel.date_of(int(frame.target_day.max())).isoformat(),
        },
        "calibration_offsets": [float(v) for v in model.quantiles.offsets],
        "fallback": {
            "model": SeasonalMovingAverage.name,
            "residual_quantiles": fallback.residuals.to_json(),
        },
        "backtest": _backtest_summary(backtest),
        "files": {name: _sha(text.encode()) for name, text in sorted(files.items())},
    }
    checksum = _content_checksum(manifest)
    manifest["artifact_checksum"] = checksum
    manifest["model_version"] = f"demand-hybrid-{checksum[:12]}"
    return manifest, files


def _backtest_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "candidate": report.get("candidate"),
        "data_version": report["data_version"],
        "origins": len(report["origins"]),
        "overall": {name: m["overall"] for name, m in report["models"].items()},
        "acceptance": report.get("acceptance"),
    }


def save_artifact(manifest: dict[str, Any], files: dict[str, str], root: Path) -> Path:
    """Write atomically to ``root/<model_version>``; an existing identical version is kept."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / str(manifest["model_version"])
    if target.exists():
        load_artifact(target)  # raises if the existing copy is corrupt
        return target
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=root))
    try:
        for name, text in files.items():
            (tmp / name).write_text(text, encoding="utf-8")
        (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def load_artifact(path: Path) -> ForecastArtifact:
    manifest_path = path / MANIFEST
    if not manifest_path.is_file():
        raise ArtifactError(f"no {MANIFEST} in {path}")
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError("manifest is not valid JSON") from exc
    if manifest.get("artifact_format") != ARTIFACT_FORMAT:
        raise ArtifactError(f"unsupported artifact format {manifest.get('artifact_format')}")
    if manifest.get("feature_version") != FEATURE_VERSION:
        raise ArtifactError(
            f"artifact feature version {manifest.get('feature_version')} != {FEATURE_VERSION}"
        )
    if _content_checksum(manifest) != manifest.get("artifact_checksum"):
        raise ArtifactError("manifest checksum mismatch")
    texts: dict[str, str] = {}
    for name, digest in manifest["files"].items():
        file = path / name
        if not file.is_file():
            raise ArtifactError(f"missing model file {name}")
        data = file.read_bytes()
        if _sha(data) != digest:
            raise ArtifactError(f"checksum mismatch for {name}")
        texts[name] = data.decode("utf-8")
    cfg = ForecastConfig.model_validate(manifest["config"])
    levels = tuple(manifest["quantiles"])
    if RIDGE_FILE not in texts:
        raise ArtifactError(f"missing model file {RIDGE_FILE}")
    boosters = {n.removesuffix(".txt"): t for n, t in texts.items() if n.endswith(".txt")}
    model = HybridForecaster(
        RidgeBaseline.from_state(levels, json.loads(texts[RIDGE_FILE])),
        LightGBMForecaster.from_model_strings(
            cfg.lightgbm, levels, boosters, manifest["calibration_offsets"], with_point=False
        ),
    )
    fallback = SeasonalMovingAverage(levels)
    fallback.residuals = ResidualQuantiles.from_json(
        levels, manifest["fallback"]["residual_quantiles"]
    )
    cat = manifest["catalogue"]
    return ForecastArtifact(
        manifest=manifest,
        model=model,
        fallback=fallback,
        catalogue=Catalogue(tuple(cat["regions"]), tuple(cat["products"]), tuple(cat["segments"])),
        config=cfg,
        series=tuple(SeriesKey(*s) for s in manifest["series"]),
    )
