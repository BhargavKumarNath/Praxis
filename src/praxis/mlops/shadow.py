"""Shadow evaluation: the challenger scored on exactly the champion's observations.

Offline gate (``[shadow]``): from the day the version entered shadow, every origin whose
seven horizons have matured is scored ONCE as a frame; champion and challenger predict that
same frame. Non-inferiority on pooled vWAPE and pinball, no slice materially worse, interval
coverage in band. Fewer than ``min_origins`` matured origins = still pending (no decision).

Online (``ShadowRecorder``): the serving router runs the shadow version on the snapshot
the champion just used and records the pair; the caller only ever receives the champion's
forecast.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from praxis.forecasting.artifact import ForecastArtifact
from praxis.forecasting.service import ForecastResult
from praxis.mlops.config import MlopsConfig
from praxis.mlops.scoring import (
    WarehouseData,
    check_compatible,
    matured_origins,
    metrics,
    observations,
    predict,
    training_end,
    worst_slice_ratio,
)
from praxis.mlops.validation import check


def evaluate_shadow(
    champion: ForecastArtifact,
    challenger: ForecastArtifact,
    data: WarehouseData,
    started_as_of: date,
    as_of: date,
    cfg: MlopsConfig,
) -> dict[str, Any]:
    """Shadow report; ``status`` = pending (too few origins) or evaluated (``passed``)."""
    sh = cfg.shadow
    check_compatible([champion, challenger])
    first = max(started_as_of - timedelta(days=1), training_end(champion), training_end(challenger))
    origins = matured_origins(
        data.panel,
        first=first,
        as_of=as_of,
        max_horizon=max(champion.config.target.horizons),
        full=True,
    )
    base: dict[str, Any] = {
        "champion": champion.model_version,
        "challenger": challenger.model_version,
        "started_as_of": started_as_of.isoformat(),
        "as_of": as_of.isoformat(),
        "origins": len(origins),
        "min_origins": sh.min_origins,
    }
    obs = observations(champion, data, origins) if origins else None
    if obs is None or len(obs.origins) < sh.min_origins:
        return {**base, "status": "pending", "passed": False, "checks": []}
    m_champ = metrics(obs, predict(champion, obs))
    m_chal = metrics(obs, predict(challenger, obs))
    c, h = m_champ["overall"], m_chal["overall"]
    v_ratio = h["vwape"] / c["vwape"]
    p_ratio = h["pinball"] / c["pinball"]
    worst, label = worst_slice_ratio(m_chal["slices"], m_champ["slices"], sh.min_slice_rows)
    checks = [
        check(
            "shadow_vwape_vs_champion",
            v_ratio <= sh.max_vwape_ratio_vs_champion,
            v_ratio,
            f"<= {sh.max_vwape_ratio_vs_champion}",
        ),
        check(
            "shadow_pinball_vs_champion",
            p_ratio <= sh.max_pinball_ratio_vs_champion,
            p_ratio,
            f"<= {sh.max_pinball_ratio_vs_champion}",
        ),
        check(
            "shadow_worst_slice_vs_champion",
            worst <= sh.max_slice_vwape_ratio_vs_champion,
            worst,
            f"<= {sh.max_slice_vwape_ratio_vs_champion}",
            slice=label,
        ),
    ]
    for key, (lo, hi) in (("80", sh.coverage_80), ("50", sh.coverage_50)):
        v = float(h[f"coverage_{key}"])
        checks.append(check(f"shadow_coverage_{key}", lo <= v <= hi, v, f"in [{lo}, {hi}]"))
    return {
        **base,
        "status": "evaluated",
        "rows": len(obs),
        "same_observations": True,
        "champion_metrics": c,
        "challenger_metrics": h,
        "checks": checks,
        "passed": all(x["passed"] for x in checks),
        "policy_version": cfg.policy_version,
    }


@dataclass
class ShadowRecorder:
    """Bounded in-process log of (champion, shadow) forecast pairs from live requests."""

    capacity: int = 1000
    pairs: deque[dict[str, Any]] = field(default_factory=deque)
    counters: Counter[str] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, champion: ForecastResult, shadow: ForecastResult) -> None:
        same_snapshot = champion.feature_date == shadow.feature_date and [
            (p.series, p.horizon_days) for p in champion.points
        ] == [(p.series, p.horizon_days) for p in shadow.points]
        diffs = [
            abs(a.point - b.point) / max(abs(a.point), 1.0)
            for a, b in zip(champion.points, shadow.points, strict=False)
        ]
        with self._lock:
            self.counters["shadow_pairs_total"] += 1
            if not same_snapshot:
                self.counters["shadow_mismatched_inputs_total"] += 1
            self.pairs.append(
                {
                    "champion": champion.model_version,
                    "shadow": shadow.model_version,
                    "feature_date": champion.feature_date.isoformat(),
                    "points": len(champion.points),
                    "same_inputs": same_snapshot,
                    "mean_relative_difference": sum(diffs) / len(diffs) if diffs else 0.0,
                }
            )
            while len(self.pairs) > self.capacity:
                self.pairs.popleft()

    def failure(self, reason: str) -> None:
        with self._lock:
            self.counters[f"shadow_failures_total:{reason}"] += 1

    def snapshot(self) -> Mapping[str, Any]:
        with self._lock:
            return {"counters": dict(self.counters), "recent": list(self.pairs)[-10:]}
