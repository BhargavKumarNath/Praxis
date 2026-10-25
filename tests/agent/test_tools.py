"""Tool schemas (s18: invalid arguments rejected BEFORE execution) and every tool's behaviour.

Read tools must report "no data" honestly for metrics nothing feeds, label the telemetry
scope, synthetic numbers and unvalidated pricing, and name only exercised runbooks as such.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from praxis.agent.audit import incomplete_calls
from praxis.agent.tools import (
    EXERCISED_RUNBOOKS,
    FEEDS,
    PRICING_NOT_VALIDATED,
    Tier,
)
from praxis.agent.wiring import TOOLS
from praxis.domain.model_lifecycle import ModelStage
from praxis.ops.runbooks import REQUIRED_RUNBOOKS
from praxis.telemetry import Telemetry
from tests.agent.conftest import NOW, World
from tests.agent.helpers import (
    CAND,
    CHAMP,
    NAME,
    OLD,
    champion_with_history,
    ok_call,
    recorded_change,
    report,
    shadow_candidate,
)

TIERS = {
    "get_system_health": 0, "get_source_freshness": 0, "get_queue_health": 0,
    "inspect_drift": 0, "inspect_forecast_error": 0, "inspect_recovery_metrics": 0,
    "compare_models": 0, "run_pricing_counterfactual": 0, "explain_decision": 0,
    "get_incident": 0, "get_runbook": 0, "open_incident": 1, "request_retraining": 1,
    "collect_diagnostics": 1, "freeze_pricing": 2, "request_rollback": 2,
    "request_promotion": 2,
}  # fmt: skip


def test_tool_catalogue_and_tiers_are_pinned() -> None:
    assert {t.name: int(t.tier) for t in TOOLS} == TIERS
    for tool in TOOLS:
        schema = tool.schema()
        assert schema["parameters"].get("additionalProperties") is False, tool.name
        assert tool.args.model_config.get("extra") == "forbid"


INVALID = [
    ("get_source_freshness", {"source": "fred; DROP TABLE x"}),
    ("explain_decision", {"decision_id": "dec-123"}),
    ("explain_decision", {}),
    ("run_pricing_counterfactual", {"decision_id": "dec-" + "0" * 20, "price_micros": -5}),
    ("inspect_recovery_metrics", {"as_of": "yesterday"}),
    ("inspect_recovery_metrics", {"as_of": "2026-08-01", "window_days": 365}),
    ("get_incident", {"incident_id": "../../etc/passwd"}),
    ("get_runbook", {"name": "../../CLAUDE"}),
    ("compare_models", {"candidate": "X" * 200}),
    ("open_incident", {"diagnosis": "rm -rf", "summary": "x", "evidence": ["call_0"]}),
    ("open_incident", {"diagnosis": "unknown", "summary": "", "evidence": []}),
    ("open_incident", {"diagnosis": "unknown", "summary": "s" * 301, "evidence": ["x"]}),
    ("freeze_pricing", {"scope": "api_requests OR 1=1", "reason": "x", "evidence": ["x"]}),
    ("request_promotion", {"model_version": "v", "reason": "x", "evidence": "call_x"}),
    ("get_system_health", {"unexpected": True}),
    ("get_system_health", ["not", "an", "object"]),
]


@pytest.mark.parametrize(("name", "args"), INVALID)
def test_invalid_arguments_are_rejected_before_execution(
    memory_world: World, name: str, args: object
) -> None:
    session = memory_world.session()
    result = memory_world.executor.call(session, name, args)
    assert result.status == "invalid" and result.data == {}
    assert memory_world.backends.incidents.store.live() == []
    assert memory_world.requests.recent() == []
    outcome = memory_world.audit.events(call_id=result.call_id)[-1]
    assert outcome.status == "invalid" and set(outcome.args) == {"keys", "sha256"}


# ------------------------------------------------------------------ read tools
def test_health_reports_no_data_and_names_the_gaps(memory_world: World) -> None:
    data = ok_call(memory_world, memory_world.session()).data
    assert data["status"] == "no_data" and data["data_origin"] == "synthetic"
    assert set(data["feeds"]) == set(FEEDS)
    assert all(v.startswith("no_data; feed: ") for v in data["feeds"].values())
    scope, monitor = data["notes"]
    assert scope.startswith("Telemetry scope: process.") and "not of the whole" in scope
    assert "ops monitor is not seen" in monitor
    assert data["inputs"]["telemetry_scope"] == "process"


def test_health_reflects_a_recorded_metric(memory_world: World, tel: Telemetry) -> None:
    tel.set("queue_backlog", 5000, subscription="praxis-operational")
    data = ok_call(memory_world, memory_world.session()).data
    assert data["feeds"]["queue_backlog"].startswith("value present")
    rules = {a["rule_id"] for a in data["sections"]["service"]["alerts"]}
    assert "queue_backlog_high" in rules


def test_freshness_and_queue_no_data_then_values(memory_world: World, tel: Telemetry) -> None:
    s = memory_world.session()
    empty = memory_world.executor.call(s, "get_source_freshness", {}).data
    assert empty["status"] == "no_data" and FEEDS["data_source_age_s"] in empty["why"]
    assert empty["collector"] == {"status": "not configured"}
    queue = memory_world.executor.call(s, "get_queue_health", {}).data
    assert queue["queue_backlog"]["status"] == "no_data" and queue["event_lag_p95_ms"] is None
    tel.set("data_source_age_s", 7_000_000, source="fred")  # limit 6,480,000 s
    tel.set("data_source_age_s", 10, source="eia")
    tel.inc("data_contract_failures_total", source="fred")
    tel.set("dlq_backlog", 12, subscription="praxis-operational")
    tel.inc("events_consumed_total", subscription="praxis-operational", outcome="applied")
    fresh = memory_world.executor.call(s, "get_source_freshness", {}).data
    assert {x["source"]: x["stale"] for x in fresh["sources"]} == {"fred": True, "eia": False}
    assert fresh["contract_failures"]["status"] == "ok"
    one = memory_world.executor.call(s, "get_source_freshness", {"source": "eia"}).data
    assert [x["source"] for x in one["sources"]] == ["eia"]
    queue = memory_world.executor.call(s, "get_queue_health", {}).data
    assert queue["dlq_backlog"]["series"][0]["value"] == 12
    assert queue["dlq_backlog"]["feed"] == FEEDS["dlq_backlog"]
    assert queue["consumer_outcomes"] == {"applied": 1}


def test_model_tools_without_registry_report_it(memory_world: World) -> None:
    s = memory_world.session()
    for name in ("inspect_drift", "inspect_forecast_error", "compare_models"):
        r = memory_world.executor.call(s, name, {})
        assert r.status == "error" and "no model registry" in (r.error or "")
    r = memory_world.executor.call(s, "inspect_recovery_metrics", {"as_of": "2026-08-01"})
    assert r.status == "error" and "no dunning read model" in (r.error or "")


def test_model_tools_read_the_registry(pg_world: World, tel: Telemetry) -> None:
    store = pg_world.backends.registry.store  # type: ignore[union-attr]
    s = pg_world.session()
    assert "no champion" in (pg_world.executor.call(s, "inspect_drift", {}).error or "")
    assert "no candidate" in (pg_world.executor.call(s, "compare_models", {}).error or "")
    champion_with_history(store)
    store.add_monitoring_report(report(CHAMP, "drift", True, {"residual_ratio": 1.4}))
    store.add_monitoring_report(report(CHAMP, "probation", False, {"vwape_ratio": 1.01}))
    store.add_validation_report(CHAMP, [], {"checks": [{"check": "x", "passed": True}]})
    store.request_retraining(NAME, ["residual_drift"], {}, date(2026, 8, 20), None, CHAMP)
    tel.set("model_drift_alarm", 1, model=NAME)
    drift = pg_world.executor.call(s, "inspect_drift", {}).data
    assert drift["champion"] == {"model_version": CHAMP, "stage": "champion"}
    assert drift["drift_reports"][-1]["fired"] is True
    assert drift["live_retraining_request"]["triggers"] == ["residual_drift"]
    err = pg_world.executor.call(s, "inspect_forecast_error", {}).data
    assert err["probation_reports"][0]["signals"] == {"vwape_ratio": 1.01}
    assert err["calibration_80"]["status"] == "no_data"
    shadow_candidate(store, regressed=True)
    cmp = pg_world.executor.call(s, "compare_models", {}).data
    assert cmp["candidate"]["model_version"] == CAND and cmp["ready_for_promotion"] is False
    assert cmp["champion"]["validation"] == {"passed": True, "failed_checks": []}
    named = pg_world.executor.call(s, "compare_models", {"candidate": OLD}).data
    assert named["candidate"]["stage"] == ModelStage.RETIRED.value
    missing = pg_world.executor.call(s, "compare_models", {"candidate": "demand-hybrid-zzz"})
    assert "unknown model version" in (missing.error or "")


def test_pricing_tools_explain_and_look_up_only(memory_world: World) -> None:
    decision = recorded_change(memory_world)
    s = memory_world.session()
    ex = memory_world.executor.call(s, "explain_decision", {"decision_id": decision.decision_id})
    assert ex.data["status"] == "change" and PRICING_NOT_VALIDATED in ex.data["notes"]
    assert "never decides" in ex.data["decided_by"] and ex.data["is_synthetic"] is True
    chosen = decision.chosen_price_micros
    cf = memory_world.executor.call(
        s, "run_pricing_counterfactual",
        {"decision_id": decision.decision_id, "price_micros": chosen},
    ).data  # fmt: skip
    assert cf["status"] == "recorded" and cf["chosen"] is True
    assert cf["evaluation"]["price_micros"] == chosen and PRICING_NOT_VALIDATED in cf["notes"]
    missing = memory_world.executor.call(s, "explain_decision", {"decision_id": "dec-" + "f" * 20})
    assert missing.status == "error" and "no recorded decision" in (missing.error or "")


def test_pricing_tools_without_store(memory_world: World) -> None:
    memory_world.backends.decisions = None
    r = memory_world.executor.call(
        memory_world.session(), "explain_decision", {"decision_id": "dec-" + "0" * 20}
    )
    assert r.status == "error" and "no pricing decision store" in (r.error or "")


def _cases(engine: Engine, start: datetime, states: dict[str, int]) -> None:
    n = 0
    with engine.begin() as conn:
        for state, count in states.items():
            for _ in range(count):
                n += 1
                conn.execute(
                    text(
                        "INSERT INTO dunning_cases (invoice_id, customer_id, state, amount_minor, "
                        "currency, opened_at, attempts_seen, recovered_at) VALUES "
                        "(:i, 'cust_1', :s, 1000, 'GBP', :o, 1, :r)"
                    ),
                    {"i": f"inv_{start.date()}_{n}", "s": state, "o": start + timedelta(hours=n),
                     "r": start + timedelta(days=1) if state == "recovered" else None},
                )  # fmt: skip


def test_recovery_metrics_compare_windows(pg_world: World, pg_engine: Engine) -> None:
    end = datetime(2026, 8, 15, tzinfo=NOW.tzinfo)
    _cases(pg_engine, end - timedelta(days=14), {"recovered": 30, "closed": 10})
    _cases(pg_engine, end - timedelta(days=7), {"recovered": 5, "closed": 20, "past_due": 3})
    data = pg_world.executor.call(
        pg_world.session(), "inspect_recovery_metrics", {"as_of": "2026-08-15"}
    ).data
    assert data["previous"]["resolved_recovery_rate"] == 0.75
    assert data["current"]["resolved_recovery_rate"] == 0.2
    assert data["current"]["still_open"] == 3 and data["current"]["opened_amount_minor"] == 28000
    empty = pg_world.executor.call(
        pg_world.session(), "inspect_recovery_metrics", {"as_of": "2020-01-01"}
    ).data
    assert empty["current"]["resolved_recovery_rate"] is None


def test_incident_and_runbook_tools(memory_world: World) -> None:
    s = memory_world.session()
    ev = ok_call(memory_world, s).call_id
    opened = memory_world.executor.call(
        s, "open_incident", {"diagnosis": "demand_spike", "summary": "kpi up", "evidence": [ev]}
    ).data
    got = memory_world.executor.call(s, "get_incident", {"incident_id": opened["incident_id"]})
    assert got.data["incident"]["rule_id"] == "agent:demand_spike"
    assert [e["kind"] for e in got.data["timeline"]] == ["opened"]
    none = memory_world.executor.call(s, "get_incident", {"incident_id": "inc_" + "0" * 16})
    assert none.status == "error"
    for name in REQUIRED_RUNBOOKS:
        rb = memory_world.executor.call(s, "get_runbook", {"name": name}).data
        assert set(rb["sections"]) >= {"Trigger", "Diagnose", "Mitigate", "Verify"}
        assert (rb["exercised_in_test"] is not None) == (name in EXERCISED_RUNBOOKS)
        if name not in EXERCISED_RUNBOOKS:
            assert "NOT exercised" in rb["validation"]


def test_missing_runbook_file(memory_world: World, tmp_path) -> None:  # type: ignore[no-untyped-def]
    memory_world.backends.runbook_dir = tmp_path
    r = memory_world.executor.call(memory_world.session(), "get_runbook", {"name": "stale-model"})
    assert r.status == "error" and "missing" in (r.error or "")


# ------------------------------------------------------------------ actions
def test_open_incident_and_diagnostics(memory_world: World) -> None:
    s = memory_world.session()
    ev = ok_call(memory_world, s).call_id
    first = memory_world.executor.call(
        s, "open_incident", {"diagnosis": "queue_backlog", "summary": "dlq", "evidence": [ev]}
    ).data
    assert first["created"] is True and first["runbook"] == "pubsub-backlog"
    assert [e for e, _ in memory_world.notifier.sent] == ["opened"]
    diag = memory_world.executor.call(
        s, "collect_diagnostics", {"incident_id": first["incident_id"], "evidence": [ev]}
    ).data
    assert "queue" in diag["attached"]
    kinds = [e["kind"] for e in memory_world.backends.incidents.store.events(first["incident_id"])]
    assert kinds == ["opened", "diagnostics"]
    s2 = memory_world.session()
    ev2 = ok_call(memory_world, s2).call_id
    again = memory_world.executor.call(
        s2, "open_incident", {"diagnosis": "queue_backlog", "summary": "dlq", "evidence": [ev2]}
    ).data
    assert again == {**first, "created": False}  # deduplicated by fingerprint
    no_issue = memory_world.executor.call(
        s2, "open_incident", {"diagnosis": "no_issue", "summary": "x", "evidence": [ev2]}
    )
    assert no_issue.status == "error"
    gone = memory_world.executor.call(
        s2, "collect_diagnostics", {"incident_id": "inc_" + "1" * 16, "evidence": [ev2]}
    )
    assert gone.status == "error" and "no live incident" in (gone.error or "")


def test_open_incident_does_not_step_other_incidents(memory_world: World) -> None:
    manager = memory_world.backends.incidents
    from praxis.ops.monitor import synthetic_alert

    manager.process([synthetic_alert(memory_world.backends.ops, "service")], NOW)
    s = memory_world.session()
    ev = ok_call(memory_world, s).call_id
    memory_world.executor.call(
        s, "open_incident", {"diagnosis": "demand_spike", "summary": "x", "evidence": [ev]}
    )
    synthetic = next(i for i in manager.store.live() if i.synthetic)
    assert synthetic.clear_streak == 0 and synthetic.live


def test_retraining_request_joins_live_request(pg_world: World) -> None:
    champion_with_history(pg_world.backends.registry.store)  # type: ignore[union-attr]
    s = pg_world.session()
    ev = ok_call(pg_world, s).call_id
    args = {"reason": "drift", "as_of": "2026-08-20", "evidence": [ev]}
    first = pg_world.executor.call(s, "request_retraining", args).data
    assert first["created"] is True and first["status"] == "open"
    second = pg_world.executor.call(s, "request_retraining", {**args, "as_of": "2026-08-21"})
    assert second.data["joined_existing"] is True
    req = pg_world.backends.registry.store.get_request(first["request_id"])  # type: ignore[union-attr]
    assert req is not None and req.triggers == ("agent_request",)
    assert req.evidence["requested_by"] == "praxis-agent" and req.champion_version == CHAMP


def test_rollback_request_then_human_approval(pg_world: World) -> None:
    reg = pg_world.backends.registry
    assert reg is not None
    s = pg_world.session()
    ev = ok_call(pg_world, s).call_id
    args = {"reason": "probation regression", "evidence": [ev]}
    assert "no champion" in (pg_world.executor.call(s, "request_rollback", args).error or "")
    champion_with_history(reg.store)
    made = pg_world.executor.call(s, "request_rollback", args).data
    assert made["kind"] == "rollback" and made["target"] == CHAMP and made["executed"] is False
    assert reg.store.one_in_stage(NAME, ModelStage.CHAMPION).model_version == CHAMP  # type: ignore[union-attr]
    done = pg_world.desk.approve(made["request_id"], "owner")
    assert done.status == "completed" and done.decided_by == "owner"
    assert reg.store.one_in_stage(NAME, ModelStage.CHAMPION).model_version == OLD  # type: ignore[union-attr]
    events = reg.store.events(decision_id=done.outcome["registry_decision_id"])  # type: ignore[index]
    assert {e.actor for e in events} == {"owner"}  # attributed to the human, not the agent


def test_stale_rollback_request_fails_safely(pg_world: World) -> None:
    reg = pg_world.backends.registry
    assert reg is not None
    champion_with_history(reg.store)
    s = pg_world.session()
    ev = ok_call(pg_world, s).call_id
    req = pg_world.executor.call(s, "request_rollback", {"reason": "x", "evidence": [ev]}).data[
        "request_id"
    ]
    reg.rollback(actor="someone-else", reason="manual")  # champion changes meanwhile
    done = pg_world.desk.approve(req, "owner")
    assert done.status == "failed" and "champion changed" in done.outcome["reason"]  # type: ignore[index]
    assert reg.store.one_in_stage(NAME, ModelStage.CHAMPION).model_version == OLD  # type: ignore[union-attr]


def test_rollback_without_target(pg_world: World) -> None:
    store = pg_world.backends.registry.store  # type: ignore[union-attr]
    from praxis.mlops.store import Move
    from tests.agent.helpers import AUDIT, record

    store.register(record(OLD), AUDIT)
    store.move(NAME, [Move(OLD, "validation.passed"), Move(OLD, "promote.bootstrap")], "d", AUDIT)
    s = pg_world.session()
    ev = ok_call(pg_world, s).call_id
    r = pg_world.executor.call(s, "request_rollback", {"reason": "x", "evidence": [ev]})
    assert r.status == "error" and "no rollback target" in (r.error or "")


def test_promotion_request_is_refused_unless_ready_then_approved(pg_world: World) -> None:
    reg = pg_world.backends.registry
    assert reg is not None
    champion_with_history(reg.store)
    shadow_candidate(reg.store, regressed=True)
    s = pg_world.session()
    ev = ok_call(pg_world, s).call_id
    args = {"model_version": CAND, "reason": "shadow", "evidence": [ev]}
    refused = pg_world.executor.call(s, "request_promotion", args)
    assert refused.status == "error" and "not ready" in (refused.error or "")
    passing = report(CAND, "shadow", False, {"champion": CHAMP})
    reg.store.add_monitoring_report(replace(passing, as_of=passing.as_of + timedelta(days=1)))
    s2 = pg_world.session()
    ev2 = ok_call(pg_world, s2).call_id
    made = pg_world.executor.call(s2, "request_promotion", {**args, "evidence": [ev2]}).data
    assert reg.store.one_in_stage(NAME, ModelStage.CHAMPION).model_version == CHAMP  # type: ignore[union-attr]
    done = pg_world.desk.approve(made["request_id"], "owner")
    assert done.status == "completed"
    assert reg.store.one_in_stage(NAME, ModelStage.CHAMPION).model_version == CAND  # type: ignore[union-attr]
    with pytest.raises(Exception, match="not pending"):
        pg_world.desk.approve(made["request_id"], "owner")


def test_freeze_request_approval_lift_and_reject(pg_world: World) -> None:
    s = pg_world.session()
    ev = ok_call(pg_world, s).call_id
    unknown = pg_world.executor.call(
        s, "freeze_pricing", {"scope": "teleport", "reason": "x", "evidence": [ev]}
    )
    assert unknown.status == "error" and "unknown product" in (unknown.error or "")
    made = pg_world.executor.call(
        s, "freeze_pricing", {"scope": "gpu_minutes", "reason": "volatility", "evidence": [ev]}
    ).data
    s2 = pg_world.session()
    ev2 = ok_call(pg_world, s2).call_id
    joined = pg_world.executor.call(
        s2, "freeze_pricing", {"scope": "gpu_minutes", "reason": "again", "evidence": [ev2]}
    ).data
    assert joined["request_id"] == made["request_id"] and joined["created"] is False
    done = pg_world.desk.approve(made["request_id"], "owner")
    assert done.status == "completed" and pg_world.freezes.is_frozen("gpu_minutes")
    assert not pg_world.freezes.is_frozen("cpu_minutes")
    freeze_id = done.outcome["freeze"]["freeze_id"]  # type: ignore[index]
    assert pg_world.freezes.lift(freeze_id, by="owner").lifted_by == "owner"
    s3 = pg_world.session()
    ev3 = ok_call(pg_world, s3).call_id
    again = pg_world.executor.call(
        s3, "freeze_pricing", {"scope": "*", "reason": "x", "evidence": [ev3]}
    ).data
    rejected = pg_world.desk.reject(again["request_id"], "owner", "not needed")
    assert rejected.status == "rejected" and pg_world.freezes.active() == []
    assert [r.status for r in pg_world.requests.recent()] == ["rejected", "completed"]
    assert incomplete_calls(pg_world.audit.events()) == []


def test_desk_without_registry_fails_model_requests(memory_world: World) -> None:
    from praxis.agent.requests import ActionRequest

    req, _ = memory_world.requests.create(
        ActionRequest("req_x", "rollback", CHAMP, {}, "x", [], "praxis-agent", "ses_x", NOW)
    )
    done = memory_world.desk.approve(req.request_id, "owner")
    assert done.status == "failed" and "no model registry" in done.outcome["reason"]  # type: ignore[index]


def test_tiers_enum_values() -> None:
    assert [int(t) for t in Tier] == [0, 1, 2]
