"""Registry-backed forecast serving: the champion pointer decides what production serves.

``RegistryForecaster`` re-reads the registry at most every ``refresh_interval_s`` (and on
demand, e.g. right after a rollback). A version is swapped in only after its artifact loads,
its checksum equals the registered checksum and its feature contract matches the serving
code; otherwise the last good version keeps serving and the refusal is counted and logged.
A rollback therefore restores the previous version on the next refresh, and an
incompatible feature contract can never be served.

The SHADOW version (if any) is scored on the snapshot the champion just used, on a
background worker after the champion's answer is ready: callers only ever receive the
champion's forecast, and a slow or failing shadow cannot change it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from praxis.domain.model_lifecycle import ModelStage
from praxis.forecasting.artifact import ArtifactError, ForecastArtifact, load_artifact
from praxis.forecasting.features import FEATURE_VERSION
from praxis.forecasting.panel import SeriesKey
from praxis.forecasting.service import (
    FeatureSource,
    ForecastMetrics,
    ForecastResult,
    ForecastService,
)
from praxis.mlops.shadow import ShadowRecorder
from praxis.mlops.store import RegistryStore, VersionRow

logger = logging.getLogger(__name__)


class ServingUnavailable(RuntimeError):
    """No servable champion has ever been loaded."""


def artifact_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme not in ("", "file"):
        raise ArtifactError(f"unsupported artifact location {parsed.scheme}://")
    return Path(unquote(parsed.path))


def load_registered(row: VersionRow) -> ForecastArtifact:
    """Load a registered version and prove it is the registered, servable artifact."""
    art = load_artifact(artifact_path(row.record.artifact_uri))
    if art.manifest["artifact_checksum"] != row.record.artifact_checksum:
        raise ArtifactError(f"{row.model_version}: artifact checksum differs from the registry")
    if art.model_version != row.model_version:
        raise ArtifactError(f"{row.model_version}: artifact is {art.model_version}")
    if art.manifest["feature_version"] != FEATURE_VERSION:
        raise ArtifactError(
            f"{row.model_version}: feature contract {art.manifest['feature_version']}"
        )
    return art


@dataclass(frozen=True)
class Active:
    champion: ForecastService
    shadow: ForecastService | None


class RegistryForecaster:
    def __init__(
        self,
        store: RegistryStore,
        model_name: str,
        source: FeatureSource,
        *,
        refresh_interval_s: float = 30.0,
        recorder: ShadowRecorder | None = None,
        service_factory: Callable[[ForecastArtifact, FeatureSource], ForecastService] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.model_name = model_name
        self.source = source
        self.refresh_interval_s = refresh_interval_s
        self.recorder = recorder or ShadowRecorder()
        self.metrics = ForecastMetrics()
        self.counters: Counter[str] = Counter()
        self._factory = service_factory or (
            lambda art, src: ForecastService(art, src, metrics=self.metrics)
        )
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._active: Active | None = None
        self._checked_at = float("-inf")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow")
        self._pending: list[Future[None]] = []

    # ------------------------------------------------------------------ pointers
    def refresh(self, *, force: bool = False) -> bool:
        """Re-read the registry; returns True when the served versions changed."""
        now = self._monotonic()
        if not force and now - self._checked_at < self.refresh_interval_s:
            return False
        self._checked_at = now
        champion = self.store.one_in_stage(self.model_name, ModelStage.CHAMPION)
        shadow = self.store.one_in_stage(self.model_name, ModelStage.SHADOW)
        current = self._active
        if champion is None:
            self.counters["refresh_no_champion_total"] += 1
            return False
        new_champion = self._service(champion, current.champion if current else None)
        if new_champion is None:
            return False
        new_shadow = None
        if shadow is not None:
            new_shadow = self._service(shadow, current.shadow if current else None)
        changed = current is None or (
            current.champion.model_version != new_champion.model_version
            or (current.shadow.model_version if current.shadow else None)
            != (new_shadow.model_version if new_shadow else None)
        )
        with self._lock:
            self._active = Active(new_champion, new_shadow)
        if changed:
            self.counters["refresh_swapped_total"] += 1
            logger.info(
                "mlops.serving_swapped",
                extra={
                    "model_name": self.model_name,
                    "model_version": new_champion.model_version,
                    "shadow_version": new_shadow.model_version if new_shadow else None,
                },
            )
        return changed

    def _service(self, row: VersionRow, current: ForecastService | None) -> ForecastService | None:
        if current is not None and current.model_version == row.model_version:
            return current
        try:
            art = load_registered(row)
        except (ArtifactError, OSError, KeyError, ValueError):
            self.counters[f"refresh_refused_total:{row.stage.value}"] += 1
            logger.exception("mlops.serving_refused", extra={"model_version": row.model_version})
            return None
        return self._factory(art, self.source)

    def _require(self) -> Active:
        self.refresh()
        active = self._active
        if active is None:
            raise ServingUnavailable(f"no servable {self.model_name} champion in the registry")
        return active

    # ------------------------------------------------------------------- serving
    @property
    def model_version(self) -> str:
        return self._require().champion.model_version

    @property
    def artifact(self) -> ForecastArtifact:
        return self._require().champion.artifact

    @property
    def shadow_version(self) -> str | None:
        active = self._require()
        return active.shadow.model_version if active.shadow else None

    def forecast(
        self,
        series: Sequence[SeriesKey] | None = None,
        horizons: Sequence[int] | None = None,
        planned_prices: Mapping[str, int] | None = None,
    ) -> ForecastResult:
        active = self._require()
        if active.shadow is None:
            return active.champion.forecast(series, horizons, planned_prices)
        snap = active.champion.fetch_snapshot()
        result = active.champion.forecast(series, horizons, planned_prices, snapshot=snap)
        shadow = active.shadow

        def run_shadow() -> None:
            try:
                self.recorder.record(
                    result, shadow.forecast(series, horizons, planned_prices, snapshot=snap)
                )
            except Exception as exc:  # the shadow must never affect production
                self.recorder.failure(type(exc).__name__)
                logger.warning("mlops.shadow_failed", extra={"model_version": shadow.model_version})

        self._pending.append(self._executor.submit(run_shadow))
        self._pending = [f for f in self._pending if not f.done()]
        return result

    def drain(self, timeout_s: float = 30.0) -> None:
        """Wait for queued shadow scoring (tests, shutdown)."""
        for f in list(self._pending):
            f.result(timeout=timeout_s)
        self._pending = [f for f in self._pending if not f.done()]

    def status(self) -> dict[str, Any]:
        active = self._active
        return {
            "model_name": self.model_name,
            "champion": active.champion.model_version if active else None,
            "shadow": active.shadow.model_version if active and active.shadow else None,
            "counters": dict(self.counters),
            "shadow_log": self.recorder.snapshot(),
        }

    def close(self) -> None:
        self._executor.shutdown(wait=True)
