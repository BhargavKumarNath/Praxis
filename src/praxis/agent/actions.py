"""Tier 1 (low-risk) and Tier 2 (guarded) tools.

Every action cites ``evidence``: call ids of successful tool calls in the same session (the
executor checks them before the handler runs), so an action is always grounded in observed
data. Tier 2 handlers NEVER act: they create a pending ``ActionRequest`` that a named human
completes with ``python -m praxis.agent approve`` (``praxis.agent.requests.ApprovalDesk``).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from praxis.agent.evidence import (
    FreshnessArgs,
    get_queue_health,
    get_source_freshness,
    get_system_health,
)
from praxis.agent.requests import ActionRequest, Kind, request_id
from praxis.agent.tools import (
    ROUTES,
    Args,
    Backends,
    Diagnosis,
    Evidence,
    IncidentId,
    ModelVersion,
    NoArgs,
    Scope,
    SessionView,
    Text,
    Tier,
    Tool,
    ToolError,
)
from praxis.domain.model_lifecycle import ModelStage
from praxis.mlops.registry import ModelRegistry
from praxis.ops.evaluate import Alert

RETRAINING_TRIGGER = "agent_request"


def _registry(b: Backends) -> ModelRegistry:
    if b.registry is None:
        raise ToolError("no model registry is configured for this agent")
    return b.registry


# ------------------------------------------------------------------ Tier 1
class OpenIncidentArgs(Args):
    diagnosis: Diagnosis
    summary: Text
    evidence: Evidence


def open_incident(b: Backends, args: OpenIncidentArgs, s: SessionView) -> dict[str, Any]:
    if args.diagnosis == "no_issue":
        raise ToolError("no_issue does not warrant an incident")
    category, runbook = ROUTES[args.diagnosis]
    alert = Alert(
        f"agent:{args.diagnosis}", category, "ticket", f"[AI operator] {args.summary}", runbook,
        observed=0.0, threshold=None,
        series={"evidence": ",".join(args.evidence), "session": s.session_id, "by": s.actor},
    )  # fmt: skip
    incident, created = b.incidents.open(alert, b.clock())
    return {"incident_id": incident.incident_id, "created": created, "runbook": runbook,
            "status": incident.status.value}  # fmt: skip


class RetrainArgs(Args):
    reason: Text
    as_of: date
    evidence: Evidence


def request_retraining(b: Backends, args: RetrainArgs, s: SessionView) -> dict[str, Any]:
    """Opens (or joins) a retraining request. Training, validation and shadow follow the
    normal MLOps lifecycle; nothing here can promote a model (ADR 0005)."""
    reg = _registry(b)
    champion = reg.store.one_in_stage(reg.model_name, ModelStage.CHAMPION)
    req, created = reg.store.request_retraining(
        reg.model_name, [RETRAINING_TRIGGER],
        {"reason": args.reason, "evidence": list(args.evidence), "requested_by": s.actor,
         "session_id": s.session_id},
        args.as_of, None, champion.model_version if champion else None,
    )  # fmt: skip
    return {"request_id": req.request_id, "created": created, "status": req.status.value,
            "joined_existing": not created,
            "note": "a candidate still needs validation, shadow and a named approver"}  # fmt: skip


class DiagnosticsArgs(Args):
    incident_id: IncidentId
    evidence: Evidence


def collect_diagnostics(b: Backends, args: DiagnosticsArgs, s: SessionView) -> dict[str, Any]:
    """Attach a health / queue / freshness snapshot to an incident's timeline."""
    incident = b.incidents.store.get(args.incident_id)
    if incident is None or not incident.live:
        raise ToolError(f"no live incident {args.incident_id}")
    health = get_system_health(b, NoArgs(), s)
    snapshot = {
        "status": health["status"],
        "alerts": [a["rule_id"] for sec in health["sections"].values() for a in sec["alerts"]],
        "queue": get_queue_health(b, NoArgs(), s),
        "freshness": get_source_freshness(b, FreshnessArgs(), s),
        "evidence": list(args.evidence),
        "collected_by": s.actor,
    }
    b.incidents.store.save(incident, "diagnostics", snapshot)
    return {"incident_id": incident.incident_id, "attached": sorted(snapshot)}


# ------------------------------------------------------------------ Tier 2 (requests only)
def _request(
    b: Backends, s: SessionView, kind: Kind, target: str, params: dict[str, Any],
    rationale: str, evidence: list[str],
) -> dict[str, Any]:  # fmt: skip
    now = b.clock()
    req, created = b.requests.create(
        ActionRequest(
            request_id(kind, target, now),
            kind,
            target,
            params,
            rationale,
            evidence,
            s.actor,
            s.session_id,
            now,
        )
    )
    return {
        "request_id": req.request_id, "kind": kind, "target": target, "status": req.status,
        "created": created, "executed": False,
        "next_step": f"a named human runs: python -m praxis.agent approve {req.request_id} "
        "--approver <name> (or reject)",
    }  # fmt: skip


class FreezeArgs(Args):
    scope: Scope
    reason: Text
    evidence: Evidence


def freeze_pricing(b: Backends, args: FreezeArgs, s: SessionView) -> dict[str, Any]:
    if args.scope != "*" and b.products is not None and args.scope not in b.products:
        raise ToolError(f"unknown product {args.scope}")
    return _request(b, s, "freeze_pricing", args.scope, {}, args.reason, list(args.evidence))


class RollbackArgs(Args):
    reason: Text
    evidence: Evidence


def request_rollback(b: Backends, args: RollbackArgs, s: SessionView) -> dict[str, Any]:
    reg = _registry(b)
    champion = reg.store.one_in_stage(reg.model_name, ModelStage.CHAMPION)
    if champion is None:
        raise ToolError("no champion to roll back")
    previous = reg.store.replaced_by(champion.model_version)
    if previous is None:
        raise ToolError(f"{champion.model_version} replaced no champion: no rollback target")
    params = {"champion_version": champion.model_version, "restore": previous}
    return _request(
        b, s, "rollback", champion.model_version, params, args.reason, list(args.evidence)
    )


class PromotionArgs(Args):
    model_version: ModelVersion
    reason: Text
    evidence: Evidence


def request_promotion(b: Backends, args: PromotionArgs, s: SessionView) -> dict[str, Any]:
    reg = _registry(b)
    ready, why = reg.ready_for_promotion(args.model_version)
    if not ready:
        raise ToolError(f"{args.model_version} is not ready for promotion: {why}")
    return _request(
        b, s, "promotion", args.model_version, {"shadow_report_id": why}, args.reason,
        list(args.evidence),
    )  # fmt: skip


ACTION_TOOLS = (
    Tool("open_incident", Tier.LOW_RISK, OpenIncidentArgs, open_incident,
         "Open (or touch) an incident for a diagnosis, citing evidence."),
    Tool("request_retraining", Tier.LOW_RISK, RetrainArgs, request_retraining,
         "Open or join a retraining request; never promotes anything."),
    Tool("collect_diagnostics", Tier.LOW_RISK, DiagnosticsArgs, collect_diagnostics,
         "Attach a health / queue / freshness snapshot to a live incident."),
    Tool("freeze_pricing", Tier.GUARDED, FreezeArgs, freeze_pricing,
         "REQUEST a price freeze (one product or '*'); a named human approves."),
    Tool("request_rollback", Tier.GUARDED, RollbackArgs, request_rollback,
         "REQUEST a rollback of the champion model; a named human approves."),
    Tool("request_promotion", Tier.GUARDED, PromotionArgs, request_promotion,
         "REQUEST promotion of a shadow version that passed; a named human approves."),
)  # fmt: skip
