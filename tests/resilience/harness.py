"""Phase 15 resilience harness: pre-registered criteria, tick-counted detection, s21 records.

``OpsProbe`` drives the REAL ops path (``OpsMonitor`` -> health -> alert evaluators ->
``IncidentManager``) on a virtual clock that advances one production tick
(``run.tick_interval_s``) per call, so a detection time is "ticks after the failure's effect
exists" x the production cadence, plus the measured wall time of the in-process chain.

``judge`` turns measurements into the s21 record and the verdict by comparing them with the
active pinned criteria, ``configs/resilience/remediation-804.toml`` (v2026-11-01.4, fault
seed 804). It supersedes ``remediation-803.toml`` (803), ``remediation.toml`` (802) and
``failures.toml`` (v1, 801), all kept unchanged with their evidence (ADR 0023 amendments
1-3). The test then asserts
the verdict, so a FAIL is both a failing test and a FAIL row in the report; a test that
errors before it is judged leaves no record and the report marks it ``FAIL (no record)``.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from praxis.ops.config import REPO_ROOT, OpsConfig, load_ops_config
from praxis.ops.evaluate import Alert
from praxis.ops.incidents import IncidentManager, IncidentStore, MemoryIncidentStore
from praxis.ops.incidents import RecordingNotifier as Notifier
from praxis.ops.monitor import OpsMonitor, Tick
from praxis.ops.system import SystemReader

CONFIG_PATH = REPO_ROOT / "configs/resilience/remediation-804.toml"
REMEDIATION_803_PATH = REPO_ROOT / "configs/resilience/remediation-803.toml"
REMEDIATION_802_PATH = REPO_ROOT / "configs/resilience/remediation.toml"
ORIGINAL_PATH = REPO_ROOT / "configs/resilience/failures.toml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunSection(_Strict):
    tick_interval_s: int
    clear_after_ticks: int
    sim_seed: int
    fault_seed: int
    burst_customers: int
    burst_days: int
    burst_duplicate_rate: float
    crash_customers: int
    crash_days: int
    crash_rate: float
    fetch_timeout_s: float
    db_statement_timeout_ms: int
    db_timeout_requests: int


class Failure(_Strict):
    id: str
    plan_item: str
    injection: str
    expected: str
    recovery: str
    alerts: tuple[str, ...]
    quiet: tuple[str, ...]
    max_ticks_to_detect: int
    required_test: str | None = None
    signal: str | None = None
    amendment: str | None = None


class Criteria(_Strict):
    version: str
    supersedes: str | None = None
    run: RunSection
    failure: tuple[Failure, ...]

    def get(self, failure_id: str) -> Failure:
        return next(f for f in self.failure if f.id == failure_id)


def load_criteria(path: Path = CONFIG_PATH) -> Criteria:
    return Criteria.model_validate(tomllib.loads(path.read_text()))


CRITERIA = load_criteria()
T0 = datetime(2026, 11, 1, 9, tzinfo=UTC)


@dataclass
class OpsProbe:
    """The real monitor on a virtual clock; records which rules fire on which tick."""

    reader: SystemReader | None = None
    store: IncidentStore = field(default_factory=MemoryIncidentStore)
    start: datetime = T0
    cfg: OpsConfig = field(default_factory=load_ops_config)
    notifier: Notifier = field(default_factory=Notifier)

    def __post_init__(self) -> None:
        self.interval = timedelta(seconds=CRITERIA.run.tick_interval_s)
        self.manager = IncidentManager(
            self.store, self.notifier, clear_after=CRITERIA.run.clear_after_ticks
        )
        self.monitor = OpsMonitor(self.cfg, self.manager, self.reader)
        self.now = self.start
        self.history: list[tuple[datetime, list[str]]] = []
        self._mark = 0
        self._wall = time.perf_counter()
        self.fired_at: dict[str, int] = {}
        self.wall_ms: dict[str, float] = {}

    def advance(self, delta: timedelta) -> None:
        """Move the virtual clock without a tick (e.g. a freshness limit elapsing)."""
        self.now += delta

    def mark(self) -> None:
        """The failure's effect exists from here: ticks are counted from the next one."""
        self._mark = len(self.history)
        self._wall = time.perf_counter()
        self.fired_at = {}
        self.wall_ms = {}

    def tick(self, extra: list[Alert] | None = None) -> Tick:
        self.now += self.interval
        tick = self.monitor.tick(self.now, extra=extra)
        firing = sorted(a.rule_id for a in tick.health.alerts())
        self.history.append((self.now, firing))
        index = len(self.history) - self._mark  # 1 = first tick after mark()
        for rule in firing:
            if rule not in self.fired_at:
                self.fired_at[rule] = index
                self.wall_ms[rule] = (time.perf_counter() - self._wall) * 1000.0
        return tick

    def firing(self) -> list[str]:
        return self.history[-1][1] if self.history else []

    def ever_fired(self) -> set[str]:
        return {r for _, rules in self.history for r in rules}

    def until_clear(self, rules: tuple[str, ...], max_ticks: int) -> int | None:
        """Tick until none of ``rules`` fires and no incident of theirs is live."""
        for n in range(1, max_ticks + 1):
            self.tick()
            live = {i.rule_id for i in self.manager.store.live()}
            if not set(rules) & (set(self.firing()) | live):
                return n
        return None


@dataclass
class Measured:
    """What a scenario observed; ``judge`` compares it with the pinned criteria."""

    failure_id: str
    actual: str
    detection_ticks: dict[str, int | None]
    detection_wall_ms: dict[str, float | None]
    quiet_fired: list[str]
    recovered: bool
    recovery_actual: str
    clear_ticks: int | None
    data_loss: int
    duplicate_side_effects: int
    signal_observed: int | None = None
    signal_expected: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Record:
    failure_id: str
    verdict: str
    reasons: list[str]
    criteria: dict[str, Any]
    measured: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"


RECORDS: dict[str, Record] = {}


def judge(m: Measured) -> Record:
    f = CRITERIA.get(m.failure_id)
    reasons: list[str] = []
    for rule in f.alerts:
        ticks = m.detection_ticks.get(rule)
        if ticks is None:
            reasons.append(f"{rule} did not fire within the detection window of its injection")
        elif ticks > f.max_ticks_to_detect:
            reasons.append(f"{rule} fired on tick {ticks} > {f.max_ticks_to_detect}")
    if m.quiet_fired:
        reasons.append(f"must stay quiet but fired: {sorted(m.quiet_fired)}")
    if f.signal is not None and (
        m.signal_observed is None or m.signal_observed != m.signal_expected
    ):
        reasons.append(
            f"signal {f.signal}: observed {m.signal_observed}, injected {m.signal_expected}"
        )
    if not m.recovered:
        reasons.append(f"did not recover: {m.recovery_actual}")
    if f.alerts and (m.clear_ticks is None or m.clear_ticks > CRITERIA.run.clear_after_ticks):
        reasons.append(
            f"alerts cleared after {m.clear_ticks} ticks > {CRITERIA.run.clear_after_ticks}"
        )
    if m.data_loss > 0:
        reasons.append(f"data loss {m.data_loss}")
    if m.duplicate_side_effects > 0:
        reasons.append(f"duplicate side effects {m.duplicate_side_effects}")
    interval = CRITERIA.run.tick_interval_s
    measured = asdict(m) | {
        "detection_time_s": {
            r: (t * interval if t is not None else None) for r, t in m.detection_ticks.items()
        },
        "data_loss_status": "none" if m.data_loss == 0 else f"{m.data_loss} lost",
        "duplicate_side_effect_status": (
            "none" if m.duplicate_side_effects == 0 else f"{m.duplicate_side_effects} duplicated"
        ),
    }
    record = Record(
        m.failure_id, "PASS" if not reasons else "FAIL", reasons, f.model_dump(), measured
    )
    RECORDS[m.failure_id] = record
    return record


def detection(
    probe: OpsProbe, rules: tuple[str, ...]
) -> tuple[dict[str, int | None], dict[str, float | None]]:
    return (
        {r: probe.fired_at.get(r) for r in rules},
        {r: (round(probe.wall_ms[r], 3) if r in probe.wall_ms else None) for r in rules},
    )


def quiet_fired(probe: OpsProbe, failure_id: str) -> list[str]:
    return sorted(set(CRITERIA.get(failure_id).quiet) & probe.ever_fired())
