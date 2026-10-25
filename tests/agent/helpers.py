"""Small, fully known control-plane states for agent tests (SYNTHETIC)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from praxis.agent.executor import Session, ToolResult
from praxis.mlops.store import Audit, MonitoringReport, Move, RegistryStore, VersionRecord
from praxis.pricing.config import Mode
from praxis.pricing.decision import Decision, Status
from praxis.pricing.optimiser import optimise
from tests.agent.conftest import World
from tests.pricing.helpers import open_policy, single_segment

NAME = "demand_forecaster"
AUDIT = Audit("tester", "test setup", "0" * 32)
OLD, CHAMP, CAND = (f"demand-hybrid-{c * 12}" for c in "abc")
AS_OF = date(2026, 8, 20)


def record(version: str) -> VersionRecord:
    return VersionRecord(
        model_name=NAME, model_version=version, artifact_uri=f"file:///models/{version}",
        artifact_checksum=version[-1] * 64, data_version="panel-1",
        feature_version="demand_features.v1", code_revision="abc123",
        params={"ridge": {"alpha": 1.0}}, metrics={"backtest": {"hybrid": {"vwape": 0.12}}},
        lineage={"training_start": "2026-01-05", "training_end": "2026-07-19", "rows": 9,
                 "config_hash": "c"},
        trained_at=datetime(2026, 7, 20, tzinfo=UTC), is_synthetic=True, reference={"bins": {}},
    )  # fmt: skip


def report(version: str, kind: str, fired: bool, signals: dict[str, object]) -> MonitoringReport:
    return MonitoringReport(
        f"{kind}-{version[-4:]}-{int(fired)}", NAME, version, kind, AS_OF,
        AS_OF - timedelta(days=14), AS_OF, fired, signals,
    )  # fmt: skip


def champion_with_history(store: RegistryStore) -> None:
    """OLD was champion, CHAMP replaced it (so a rollback target exists)."""
    for v in (OLD, CHAMP):
        store.register(record(v), AUDIT)
    store.move(NAME, [Move(OLD, "validation.passed"), Move(OLD, "promote.bootstrap")], "d1", AUDIT)
    store.move(NAME, [Move(CHAMP, "validation.passed"), Move(CHAMP, "shadow.started")], "d2", AUDIT)
    store.move(NAME, [Move(OLD, "replaced"), Move(CHAMP, "promote")], "d3", AUDIT)


def shadow_candidate(store: RegistryStore, *, regressed: bool) -> None:
    store.register(record(CAND), AUDIT)
    store.move(NAME, [Move(CAND, "validation.passed"), Move(CAND, "shadow.started")], "d4", AUDIT)
    signals = {"champion": CHAMP, "vwape_ratio": 1.12 if regressed else 0.95}
    store.add_monitoring_report(report(CAND, "shadow", regressed, signals))


def recorded_change(world: World, mode: Mode = Mode.RECOMMEND) -> Decision:
    """A real optimiser decision (tests may import the optimiser; the agent may not)."""
    pol = open_policy(max_step=0.05).with_mode(mode, allow_execute=True)
    decision = optimise(single_segment(-0.5, 10_000.0, 100_000), pol)
    assert decision.status is Status.CHANGE
    assert world.backends.decisions is not None
    world.backends.decisions.record(decision)
    return decision


def ok_call(world: World, session: Session, tool: str = "get_system_health") -> ToolResult:
    result = world.executor.call(session, tool, {})
    assert result.ok, result
    return result
