"""Pricing view: current and proposed prices, the forecast and its uncertainty, elasticity, the
objective curve over the evaluated candidates, constraints and recent decisions.

Everything is read from the decision records the pricing cycle wrote BEFORE anything executed
(``pricing_decisions.record``, the canonical audit JSON) and from ``price_executions``. The
console never re-runs the optimiser: a value the cycle did not record is not shown.

Every pricing result is a SHADOW evaluation, not validated for execution (Phase 4 loader
defect; ``SHADOW_NOTE``). Prices are integer micros of GBP per billing unit.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from praxis.console.context import ConsoleContext
from praxis.console.panels import SHADOW_NOTE, SYNTHETIC_NOTE, Data, jsonable, panel, rows

LATEST_SQL = """
SELECT DISTINCT ON (d.product)
       d.decision_id, d.cycle_id, d.product, d.as_of, d.mode, d.status,
       d.current_price_micros, d.chosen_price_micros, d.policy_version, d.reason_codes,
       d.recorded_at, d.trace_id,
       d.record->'prediction' AS prediction,
       d.record->'inputs'->'elasticity' AS elasticity,
       d.record->'inputs'->'forecast_relative_width' AS forecast_relative_width,
       d.record->'lineage' AS lineage,
       d.record->'guardrails'->'passed' AS guardrails_passed,
       e.executed_at, e.executed_by
FROM pricing_decisions d LEFT JOIN price_executions e USING (decision_id)
ORDER BY d.product, d.as_of DESC, d.recorded_at DESC
"""

HISTORY_SQL = """
SELECT product, as_of, mode, status, current_price_micros, chosen_price_micros, decision_id,
       (record->'prediction'->>'delta_objective_mean')::float AS delta_mean,
       (record->'prediction'->>'delta_objective_p05')::float AS delta_p05,
       (record->'prediction'->>'delta_objective_p95')::float AS delta_p95,
       (record->'prediction'->>'expected_demand')::float AS expected_demand
FROM (
    SELECT *, row_number() OVER (PARTITION BY product ORDER BY as_of DESC, recorded_at DESC) AS n
    FROM pricing_decisions
) d
WHERE n <= :n
ORDER BY product, as_of
"""

RECENT_SQL = """
SELECT d.decision_id, d.product, d.as_of, d.mode, d.status, d.current_price_micros,
       d.chosen_price_micros, d.policy_version, d.reason_codes, d.recorded_at,
       e.executed_at IS NOT NULL AS executed
FROM pricing_decisions d LEFT JOIN price_executions e USING (decision_id)
ORDER BY d.recorded_at DESC, d.decision_id
LIMIT :n
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _latest(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(conn.execute(text(LATEST_SQL)))
    for r in found:
        for key in ("prediction", "elasticity", "lineage", "forecast_relative_width"):
            r[key] = _json(r[key])
        r["guardrails_passed"] = _json(r["guardrails_passed"])
    return {"products": found} if found else None


def _history(ctx: ConsoleContext, per_product: int = 60) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(conn.execute(text(HISTORY_SQL), {"n": per_product}))
    out: dict[str, list[Data]] = {}
    for r in found:
        out.setdefault(r.pop("product"), []).append(r)
    return {"products": out, "per_product": per_product} if out else None


def _recent(ctx: ConsoleContext, limit: int = 40) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(conn.execute(text(RECENT_SQL), {"n": limit}))
    return {"decisions": found} if found else None


def _status_counts(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(
            conn.execute(
                text(
                    "SELECT status, mode, count(*) AS n, max(as_of) AS last_as_of "
                    "FROM pricing_decisions GROUP BY 1, 2 ORDER BY 1, 2"
                )
            )
        )
    return {"counts": found} if found else None


def pricing_view(ctx: ConsoleContext) -> Data:
    none = (
        "no pricing decision has been recorded (`python -m praxis.pricing decide "
        "--database-url ...`)"
    )
    return {
        "view": "pricing",
        "generated_at": ctx.clock().isoformat(),
        "data_origin": "synthetic",
        "shadow": True,
        "note": f"{SHADOW_NOTE}. {SYNTHETIC_NOTE}",
        "panels": {
            "latest": panel(
                "postgres: pricing_decisions, price_executions", lambda: _latest(ctx), empty=none
            ),
            "history": panel("postgres: pricing_decisions", lambda: _history(ctx), empty=none),
            "recent": panel("postgres: pricing_decisions", lambda: _recent(ctx), empty=none),
            "status_counts": panel(
                "postgres: pricing_decisions", lambda: _status_counts(ctx), empty=none
            ),
        },
    }


def decision_detail(ctx: ConsoleContext, decision_id: str) -> Data | None:
    """The recorded decision: reasons, constraints, guardrails, candidates (the objective
    curve), prediction interval and lineage (model versions), or ``None``."""
    with ctx.db().connect() as conn:
        row = conn.execute(
            text(
                "SELECT d.record, d.record_sha256, d.trace_id, d.recorded_at, "
                "e.executed_at, e.executed_by, e.from_price_micros, e.to_price_micros "
                "FROM pricing_decisions d LEFT JOIN price_executions e USING (decision_id) "
                "WHERE d.decision_id = :i"
            ),
            {"i": decision_id},
        ).first()
    if row is None:
        return None
    record = _json(row.record)
    execution = None
    if row.executed_at is not None:
        execution = jsonable(
            {
                "executed_at": row.executed_at,
                "executed_by": row.executed_by,
                "from_price_micros": row.from_price_micros,
                "to_price_micros": row.to_price_micros,
            }
        )
    return {
        "decision": record,
        "record_sha256": row.record_sha256,
        "trace_id": row.trace_id,
        "recorded_at": jsonable(row.recorded_at),
        "execution": execution,
        "shadow": True,
        "note": f"{SHADOW_NOTE}. {SYNTHETIC_NOTE}",
    }
