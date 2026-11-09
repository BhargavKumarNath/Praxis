"""The operator's playbook: how one investigation goes, stated once (ADR 0029 amendment 4).

Both investigators behind the operator follow it. The deterministic triage
(``praxis.agent.triage``) executes these tables; an LLM provider receives them rendered as
text (``render_procedure``) in its system prompt. Before this module the procedure lived
only inside triage, so the operator's two providers ran under different policies and the
LLM was scored on a procedure it was never told (amendment 3's 2/7).

The text states the procedure, never a scenario: it is built from the alert-rule sets, the
diagnosis -> runbook routes (``ROUTES``) and the triage thresholds of ``policy.toml``.
"""

from __future__ import annotations

from praxis.agent.config import TriageSection
from praxis.agent.tools import ROUTES

DATA_RULES = frozenset({"source_stale", "data_contract_failure"})
QUEUE_RULES = frozenset({"queue_backlog_high", "dlq_backlog_high", "consumer_lag_slo_breach"})
FORECAST_RULES = frozenset({"calibration_regression", "business_kpi_shock", "forecast_stale"})
MAX_ATTEMPTS = 2  # a failed tool is retried once, then the investigation goes on without it

# Diagnoses whose playbook action is opening an incident.
INCIDENT_DIAGNOSES = frozenset({"data_source_stale", "demand_spike", "recovery_collapse"})


def _rules(rules: frozenset[str], sep: str = ", ") -> str:
    return sep.join(sorted(rules))


def render_procedure(cfg: TriageSection) -> str:
    """The playbook as instructions. Every branch here is a branch of ``triage``."""
    runbooks = "; ".join(
        f"{d} -> {runbook}" for d, (_, runbook) in sorted(ROUTES.items()) if d != "unknown"
    )
    return f"""OPERATING PROCEDURE (the deterministic fallback follows the same one).
1. Evidence. Call get_system_health first. If the task has an incident_id, call get_incident
   with it. For a review_candidate task, call compare_models with the task's model_version and
   go to step 2. Otherwise call, for the alerts firing in get_system_health:
   - {_rules(DATA_RULES)}: get_source_freshness
   - {_rules(QUEUE_RULES)}: get_queue_health
   - {_rules(FORECAST_RULES)}: inspect_forecast_error and inspect_drift
   - model_drift: inspect_drift
   If the task has a decision_id, call explain_decision with it. Then always call
   inspect_recovery_metrics with as_of = the task's as_of and window_days = \
{cfg.recovery_window_days}.
   Retry a failed tool at most once, then go on without it.
2. Diagnosis: the FIRST rule below that holds.
   - data_source_stale: a {_rules(DATA_RULES, " or ")} alert is firing.
   - queue_backlog: a {_rules(QUEUE_RULES, " or ")} alert is firing.
   - model_drift: a model_drift alert is firing, or the latest inspect_drift report has
     fired = true.
   - price_volatility: a price_volatility alert is firing.
   - demand_spike: a business_kpi_shock alert is firing.
   - recovery_collapse: in inspect_recovery_metrics both windows have at least \
{cfg.recovery_min_resolved} resolved
     cases (recovered + closed) and the current resolved_recovery_rate is below
     {cfg.recovery_collapse_ratio} x the previous one.
   - no_issue: none of the above.
   For a review_candidate task: no_issue if compare_models reports ready_for_promotion =
   true, otherwise candidate_regression. Use unknown only when the evidence the rules need
   could not be retrieved.
3. Runbook. Before any action, read the runbook of the diagnosis with get_runbook:
   {runbooks}. no_issue has no runbook.
4. Action. Take only the action below for the diagnosis, and no other tier >= 1 tool. Pass
   the call ids of your successful calls as its evidence.
   - {", ".join(sorted(INCIDENT_DIAGNOSES))}: open_incident with the diagnosis and a summary.
   - queue_backlog: collect_diagnostics on the live incident whose rule_id is a queue alert
     (get_system_health lists live incidents); if there is none, open_incident.
   - model_drift: request_retraining with a reason and the task's as_of, UNLESS
     inspect_drift shows a live_retraining_request: then take no action.
   - price_volatility: freeze_pricing with scope "*" (it only creates a pending request).
   - no_issue on a review_candidate task: request_promotion for that model_version.
   - candidate_regression, and no_issue on any other task: no action.
5. Finish with the diagnosis; recommended_action is the name of the action tool you called
   in step 4, or "" if you took none."""
