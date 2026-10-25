"""Pinned, pre-registered ops configuration (Phase 11). Editing these needs an ADR + approval."""

from __future__ import annotations

import tomllib
from collections.abc import Callable

import pytest

from praxis.ops.config import REPO_ROOT, OpsConfig, Rule, Slo, load_ops_config
from praxis.ops.runbooks import REQUIRED_RUNBOOKS, validate_runbooks


@pytest.fixture(scope="module")
def cfg() -> OpsConfig:
    return load_ops_config()


def test_versions_and_counts_are_pinned(cfg: OpsConfig) -> None:
    assert (cfg.slo_version, cfg.alert_version, cfg.dashboard_version) == (
        "2026-10-25.1",
        "2026-10-25.1",
        "2026-10-25.1",
    )
    assert len(cfg.slos) == 6 and len(cfg.rules) == 14


def test_slo_targets_are_the_preregistered_values(cfg: OpsConfig) -> None:
    got = {s.id: (s.target, s.max_ms, s.max_value, s.quantile) for s in cfg.slos}
    assert got == {
        "api_availability": (0.995, None, None, None),
        "webhook_processing_success": (0.99, None, None, None),
        "pricing_decision_latency_p95": (None, 2500, None, 0.95),
        "event_consumer_lag_p95": (None, 5000, None, 0.95),
        "forecast_freshness": (None, None, 48, None),
        "dlq_backlog": (None, None, 10, None),
    }


def test_alert_thresholds_are_the_preregistered_values(cfg: OpsConfig) -> None:
    got = {r.id: r.threshold for r in cfg.rules if r.kind == "threshold" and r.threshold_by is None}
    assert got == {
        "queue_backlog_high": 1000,
        "inference_failures": 0.05,
        "data_contract_failure": 0,
        "model_drift": 1,
        "calibration_regression": 0.70,
        "business_kpi_shock": 0.30,
        "price_volatility": 0.50,
    }


def test_every_category_has_an_alert_with_a_runbook(cfg: OpsConfig) -> None:
    assert {r.category for r in cfg.rules} == {"service", "data", "model", "business"}
    assert {s.category for s in cfg.slos} >= {"service", "model"}


def test_source_stale_limits_match_the_data_freshness_contract(cfg: OpsConfig) -> None:
    sources = tomllib.loads((REPO_ROOT / "configs/data/sources.toml").read_text())["freshness"]
    rule = next(r for r in cfg.rules if r.id == "source_stale")
    assert rule.threshold_by is not None
    assert rule.threshold_by.values == {k: v * 3600 for k, v in sources.items()}


def test_runbooks_are_complete_and_consistent(cfg: OpsConfig) -> None:
    assert validate_runbooks(cfg) == []
    assert set(REQUIRED_RUNBOOKS) == {
        "external-source-outage", "stripe-webhook-surge", "stale-model",
        "broken-candidate-deployment", "database-outage", "pubsub-backlog",
        "severe-forecast-error", "agent-failure",
    }  # fmt: skip


def test_validate_runbooks_reports_problems(cfg: OpsConfig, tmp_path: object) -> None:
    from pathlib import Path

    d = Path(str(tmp_path))
    (d / "stale-model.md").write_text("# x\n\nAlerts: nope\n\n## Trigger\n")
    problems = validate_runbooks(cfg, d)
    assert "stale-model: lists unknown alert nope" in problems
    assert "stale-model: missing section '## Verify'" in problems
    assert "stale-model: does not list alert model_drift" in problems
    assert "database-outage: runbook file missing" in problems


def _bad_slo(**kw: object) -> Slo:
    base: dict[str, object] = {"id": "a", "category": "service", "description": "d"}
    return Slo.model_validate(base | kw)


def _bad_rule(**kw: object) -> Rule:
    base: dict[str, object] = {
        "id": "r", "category": "data", "severity": "page", "runbook": "x", "summary": "s",
    }  # fmt: skip
    return Rule.model_validate(base | kw)


@pytest.mark.parametrize(
    "build",
    [
        lambda: _bad_slo(kind="ratio", target=0.9),
        lambda: _bad_slo(kind="gauge_max", metric="nope", max_value=1),
        lambda: _bad_rule(kind="slo"),
        lambda: _bad_rule(kind="threshold", metric="queue_backlog", op=">"),
    ],
)
def test_invalid_definitions_are_rejected(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match=r"slo|rule|metric"):
        build()


def test_config_cross_references_are_checked(cfg: OpsConfig) -> None:
    bad = cfg.model_dump()
    bad["rules"] = [*bad["rules"], dict(bad["rules"][0])]
    with pytest.raises(ValueError, match="duplicate"):
        OpsConfig.model_validate(bad)
    bad = cfg.model_dump()
    bad["rules"][0]["slo"] = "missing"
    with pytest.raises(ValueError, match="unknown slos"):
        OpsConfig.model_validate(bad)
    bad = cfg.model_dump()
    bad["panels"][0]["metric"] = "missing"
    with pytest.raises(ValueError, match="unknown metric"):
        OpsConfig.model_validate(bad)
