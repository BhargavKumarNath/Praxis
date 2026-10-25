"""Deterministic triage: a rule-based investigator behind the provider interface.

It is NOT an LLM. It is the operator's fallback when the provider is unavailable and the
reference investigator of the incident benchmark. It reads only STRUCTURED tool output
(alert rule ids, numbers, flags), never free text, so text in incidents, task notes,
customer metadata or tool output cannot steer it.

Order: gather evidence (health, then the tools the firing alerts point at), diagnose, read
the runbook, take at most one playbook action, answer citing the successful call ids.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from praxis.agent.config import TriageSection
from praxis.agent.provider import Conversation, Final, Step, Task, ToolCall
from praxis.agent.tools import ROUTES

DATA_RULES = {"source_stale", "data_contract_failure"}
QUEUE_RULES = {"queue_backlog_high", "dlq_backlog_high", "consumer_lag_slo_breach"}
FORECAST_RULES = {"calibration_regression", "business_kpi_shock", "forecast_stale"}
MAX_ATTEMPTS = 2  # a failed tool is retried once, then the investigation goes on without it

Plan = list[tuple[str, dict[str, Any]]]


def _latest(conv: Conversation) -> dict[str, dict[str, Any]]:
    return {m["tool"]: m for m in conv.tool_messages()}


def _data(done: dict[str, dict[str, Any]], tool: str) -> dict[str, Any]:
    m = done.get(tool)
    return m["data"] if m and m["status"] == "ok" else {}


def _alerts(done: dict[str, dict[str, Any]]) -> set[str]:
    sections = _data(done, "get_system_health").get("sections", {})
    return {str(a["rule_id"]) for s in sections.values() for a in s.get("alerts", [])}


def _evidence_plan(task: Task, done: dict[str, dict[str, Any]], window: int) -> Plan:
    plan: Plan = [("get_system_health", {})]
    if task.incident_id:
        plan.append(("get_incident", {"incident_id": task.incident_id}))
    if task.kind == "review_candidate":
        plan.append(("compare_models", {"candidate": task.model_version}))
        return plan
    alerts = _alerts(done)
    if alerts & DATA_RULES:
        plan.append(("get_source_freshness", {}))
    if alerts & QUEUE_RULES:
        plan.append(("get_queue_health", {}))
    if alerts & FORECAST_RULES:
        plan.append(("inspect_forecast_error", {}))
    if alerts & (FORECAST_RULES | {"model_drift"}):
        plan.append(("inspect_drift", {}))
    if task.decision_id:
        plan.append(("explain_decision", {"decision_id": task.decision_id}))
    plan.append(
        ("inspect_recovery_metrics", {"as_of": task.as_of.isoformat(), "window_days": window})
    )
    return plan


def _drift_fired(done: dict[str, dict[str, Any]]) -> bool:
    reports = _data(done, "inspect_drift").get("drift_reports", [])
    return bool(reports) and bool(reports[-1].get("fired"))


def _recovery_collapsed(done: dict[str, dict[str, Any]], cfg: TriageSection) -> bool:
    d = _data(done, "inspect_recovery_metrics")
    cur, prev = d.get("current", {}), d.get("previous", {})
    enough = all(
        (w.get("recovered", 0) + w.get("closed", 0)) >= cfg.recovery_min_resolved
        for w in (cur, prev)
    )
    if not enough or not prev.get("resolved_recovery_rate"):
        return False
    rate, before = cur["resolved_recovery_rate"], prev["resolved_recovery_rate"]
    return bool(rate < cfg.recovery_collapse_ratio * before)


def _candidate_diagnosis(done: dict[str, dict[str, Any]]) -> str:
    compare = _data(done, "compare_models")
    if not compare:
        return "unknown"
    return "no_issue" if compare.get("ready_for_promotion") else "candidate_regression"


def diagnose(task: Task, done: dict[str, dict[str, Any]], cfg: TriageSection) -> str:
    if task.kind == "review_candidate":
        return _candidate_diagnosis(done)
    alerts = _alerts(done)
    rules = (
        (bool(alerts & DATA_RULES), "data_source_stale"),
        (bool(alerts & QUEUE_RULES), "queue_backlog"),
        ("model_drift" in alerts or _drift_fired(done), "model_drift"),
        ("price_volatility" in alerts, "price_volatility"),
        ("business_kpi_shock" in alerts, "demand_spike"),
        (_recovery_collapsed(done, cfg), "recovery_collapse"),
    )
    return next((name for hit, name in rules if hit), "no_issue")


def _live_incident(done: dict[str, dict[str, Any]], rules: set[str]) -> str | None:
    incidents = _data(done, "get_system_health").get("incidents", [])
    return next((i["incident_id"] for i in incidents if i.get("rule_id") in rules), None)


Action = tuple[str, dict[str, Any]]
INCIDENT_DIAGNOSES = {"data_source_stale", "demand_spike", "recovery_collapse"}


def _queue_action(done: dict[str, dict[str, Any]], summary: str) -> Action:
    incident = _live_incident(done, QUEUE_RULES)
    if incident is not None:
        return "collect_diagnostics", {"incident_id": incident}
    return "open_incident", {"diagnosis": "queue_backlog", "summary": summary}


def _drift_action(task: Task, done: dict[str, dict[str, Any]], summary: str) -> Action | None:
    if _data(done, "inspect_drift").get("live_retraining_request"):
        return None  # already requested: joining it would be an unnecessary action
    return "request_retraining", {"reason": summary, "as_of": task.as_of.isoformat()}


def _action(task: Task, diagnosis: str, done: dict[str, dict[str, Any]]) -> Action | None:
    summary = f"{diagnosis} found by deterministic triage"
    if diagnosis in INCIDENT_DIAGNOSES:
        return "open_incident", {"diagnosis": diagnosis, "summary": summary}
    if diagnosis == "queue_backlog":
        return _queue_action(done, summary)
    if diagnosis == "model_drift":
        return _drift_action(task, done, summary)
    if diagnosis == "price_volatility":
        return "freeze_pricing", {"scope": "*", "reason": summary}
    if diagnosis == "no_issue" and task.kind == "review_candidate" and task.model_version:
        return "request_promotion", {"model_version": task.model_version, "reason": summary}
    return None


class TriageProvider:
    name = "deterministic-triage"

    def __init__(self, cfg: TriageSection) -> None:
        self.cfg = cfg

    def next_step(self, conversation: Conversation) -> Step:
        task = conversation.task
        done = _latest(conversation)
        attempts = Counter(m["tool"] for m in conversation.tool_messages())
        allowed = {t["name"] for t in conversation.tools}
        plan = _evidence_plan(task, done, self.cfg.recovery_window_days)
        for tool, args in plan:
            pending = tool not in done or (
                done[tool]["status"] == "error" and attempts[tool] < MAX_ATTEMPTS
            )
            if tool in allowed and pending:
                return ToolCall(tool, args)
        diagnosis = diagnose(task, done, self.cfg)
        runbook = ROUTES.get(diagnosis, (None, None))[1]
        if runbook and "get_runbook" not in done and "get_runbook" in allowed:
            return ToolCall("get_runbook", {"name": runbook})
        evidence = tuple(m["call_id"] for m in conversation.tool_messages() if m["status"] == "ok")[
            :10
        ]
        action = _action(task, diagnosis, done)
        if action is not None and action[0] in allowed and action[0] not in done:
            return ToolCall(action[0], {**action[1], "evidence": list(evidence)})
        taken = action[0] if action and _data(done, action[0]) else None
        return Final(
            diagnosis,
            f"{diagnosis}: alerts {sorted(_alerts(done))}; action {taken or 'none'}"
            + (f"; runbook docs/runbooks/{runbook}.md" if runbook else ""),
            evidence,
            taken,
        )
