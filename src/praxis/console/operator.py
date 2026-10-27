"""AI Operator view: incidents, investigation traces (from the append-only audit), guarded
requests, approvals, price freezes and the audit trail.

The investigator is the DETERMINISTIC TRIAGE (``agent.triage``), a rule-based program, not an
LLM: no live LLM provider is configured (owner decision, ADR 0019). Every payload says so.

An investigation is linked to an incident when one of its calls named the incident
(``get_incident`` / ``collect_diagnostics`` arguments), when it opened the incident (the
``agent:`` incident's context names the session), or when it was started for it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from praxis.agent.audit import AuditEvent, PostgresAuditStore
from praxis.agent.config import load_agent_policy
from praxis.agent.requests import ActionRequest, PostgresRequestStore
from praxis.console.context import ConsoleContext
from praxis.console.panels import SYNTHETIC_NOTE, Data, jsonable, panel, rows
from praxis.ops.incidents import PostgresIncidentStore
from praxis.pricing.freeze import PostgresFreezeStore

INVESTIGATOR_NOTE = (
    "The investigator is deterministic triage: a rule-based program over structured tool "
    "fields, not an LLM. No live LLM provider is configured (owner decision, ADR 0019). Its "
    "benchmark measures the harness and safety layer, not LLM diagnosis."
)


def provider() -> Data:
    policy = load_agent_policy()
    agent = policy.agent
    return {
        "policy_version": policy.version,
        "actor": agent.actor,
        "enabled": agent.enabled,
        "provider": agent.provider,
        "fallback": agent.fallback,
        "is_llm": False,
        "max_tier": agent.max_tier,
        "max_actions": agent.max_actions,
        "note": INVESTIGATOR_NOTE,
    }


def _requests(found: list[ActionRequest]) -> list[Data]:
    return [r.as_dict() for r in found]


def _freezes(ctx: ConsoleContext) -> Data | None:
    active = [f.as_dict() for f in PostgresFreezeStore(ctx.db()).active()]
    with ctx.db().connect() as conn:
        lifted = rows(
            conn.execute(
                text(
                    "SELECT freeze_id, scope, reason, requested_by, approved_by, started_at, "
                    "lifted_at, lifted_by FROM pricing_freezes WHERE lifted_at IS NOT NULL "
                    "ORDER BY lifted_at DESC LIMIT 20"
                )
            )
        )
    return {"active": active, "lifted": lifted} if active or lifted else None


SESSIONS_SQL = """
SELECT s.session_id,
       min(s.at) AS started_at,
       max(s.at) AS ended_at,
       (array_agg(s.detail->>'task' ORDER BY s.event_id)
            FILTER (WHERE s.status = 'started'))[1] AS task,
       (array_agg(s.status ORDER BY s.event_id DESC)
            FILTER (WHERE s.status <> 'started'))[1] AS outcome,
       (array_agg(s.detail->>'diagnosis' ORDER BY s.event_id DESC)
            FILTER (WHERE s.detail ? 'diagnosis'))[1] AS diagnosis,
       (array_agg(s.detail->>'grounded' ORDER BY s.event_id DESC)
            FILTER (WHERE s.detail ? 'grounded'))[1] AS grounded,
       (SELECT count(*) FROM agent_audit_events c
         WHERE c.session_id = s.session_id AND c.phase = 'outcome') AS calls,
       (SELECT count(*) FROM agent_audit_events c
         WHERE c.session_id = s.session_id AND c.phase = 'outcome' AND c.tier >= 1) AS actions
FROM agent_audit_events s
WHERE s.phase = 'session' AND (CAST(:ids AS TEXT[]) IS NULL OR s.session_id = ANY(:ids))
GROUP BY s.session_id
ORDER BY min(s.at) DESC
LIMIT :n
"""


def _sessions(ctx: ConsoleContext, ids: list[str] | None = None, limit: int = 20) -> list[Data]:
    with ctx.db().connect() as conn:
        found = rows(conn.execute(text(SESSIONS_SQL), {"ids": ids, "n": limit}))
    for s in found:
        g = s.get("grounded")
        s["grounded"] = None if g is None else g == "true"
    return found


def operator_view(ctx: ConsoleContext) -> Data:
    now = ctx.clock()
    return {
        "view": "operator",
        "generated_at": now.isoformat(),
        "data_origin": "synthetic",
        "note": f"{INVESTIGATOR_NOTE} {SYNTHETIC_NOTE}",
        "panels": {
            "provider": panel(
                "config: configs/agent/policy.toml", provider, empty="", synthetic=False
            ),
            "incidents": panel(
                "postgres: ops_incidents",
                lambda: _list(
                    "incidents", [i.as_dict() for i in PostgresIncidentStore(ctx.db()).recent(50)]
                ),
                empty="no incident has been opened (the ops monitor opens them from firing "
                "alerts: `python -m praxis.ops run`)",
            ),
            "requests": panel(
                "postgres: agent_action_requests",
                lambda: _list("requests", _requests(PostgresRequestStore(ctx.db()).recent(50))),
                empty="the operator has not requested any guarded action",
            ),
            "freezes": panel(
                "postgres: pricing_freezes",
                lambda: _freezes(ctx),
                empty="no price freeze has ever been active",
            ),
            "sessions": panel(
                "postgres: agent_audit_events",
                lambda: _list("sessions", _sessions(ctx)),
                empty="no investigation has run (`python -m praxis.agent investigate ...`)",
            ),
        },
    }


def _list(key: str, items: list[Data]) -> Data | None:
    return {key: items} if items else None


LINKED_SQL = """
SELECT DISTINCT session_id FROM agent_audit_events
 WHERE phase = 'outcome' AND args->>'incident_id' = :i
UNION
SELECT context->'series'->>'session' FROM ops_incidents
 WHERE incident_id = :i AND context->'series'->>'session' IS NOT NULL
"""


def _calls(events: list[AuditEvent]) -> list[Data]:
    """One entry per call: received and outcome rows folded together, in call order."""
    calls: dict[str, Data] = {}
    for e in events:
        if e.phase == "session":
            continue
        c = calls.setdefault(
            e.call_id,
            {
                "call_id": e.call_id,
                "tool": e.tool,
                "tier": e.tier,
                "received_at": None,
                "outcome_at": None,
                "status": None,
                "args": {},
                "error": None,
            },
        )
        if e.phase == "received":
            c["received_at"] = e.at.isoformat()
        else:
            c.update(
                outcome_at=e.at.isoformat(),
                status=e.status,
                args=e.args,
                error=e.detail.get("error"),
            )
    return list(calls.values())


def session_trace(ctx: ConsoleContext, session_id: str) -> Data | None:
    """The full audit trail of one investigation, or ``None`` if it does not exist."""
    events = PostgresAuditStore(ctx.db()).events(session_id=session_id)
    if not events:
        return None
    summary = _sessions(ctx, [session_id], limit=1)
    with ctx.db().connect() as conn:
        requests = rows(
            conn.execute(
                text(
                    "SELECT request_id FROM agent_action_requests WHERE session_id = :s "
                    "ORDER BY created_at"
                ),
                {"s": session_id},
            )
        )
    store = PostgresRequestStore(ctx.db())
    found = [store.get(r["request_id"]) for r in requests]
    return {
        "session": summary[0] if summary else {"session_id": session_id},
        "session_events": [e.as_dict() for e in events if e.phase == "session"],
        "calls": _calls(events),
        "requests": _requests([r for r in found if r is not None]),
        "note": INVESTIGATOR_NOTE,
    }


def incident_detail(ctx: ConsoleContext, incident_id: str) -> Data | None:
    store = PostgresIncidentStore(ctx.db())
    incident = store.get(incident_id)
    if incident is None:
        return None
    with ctx.db().connect() as conn:
        linked = [r[0] for r in conn.execute(text(LINKED_SQL), {"i": incident_id})]
    traces: list[Any] = [session_trace(ctx, s) for s in sorted(linked)]
    return {
        "incident": incident.as_dict(),
        "events": jsonable(store.events(incident_id)),
        "investigations": sorted(
            (t for t in traces if t is not None),
            key=lambda t: str(t["session"].get("started_at") or ""),
        ),
        "note": INVESTIGATOR_NOTE,
    }
