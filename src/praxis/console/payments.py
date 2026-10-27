"""Payments view: failed payments, scheduled retries, recovery probability, time to recovery,
recovered revenue and dunning state.

Read from the control-plane stores of record: ``invoices``, ``dunning_cases``,
``dunning_decisions`` (each retry decision with its model or baseline version and the model's
``p_next_success``), ``retry_jobs`` and ``payment_ledger``. All SYNTHETIC. Times are the
simulated event times the cases carry, not wall-clock time. Amounts are integer minor units.

The recovery rate uses the same definition as the AI operator's ``inspect_recovery_metrics``
(``dunning.store.RecoveryWindow``): recovered / (recovered + closed); open cases are excluded.
"""

from __future__ import annotations

from sqlalchemy import text

from praxis.console.context import ConsoleContext
from praxis.console.panels import SYNTHETIC_NOTE, Data, panel, rows

SUMMARY_SQL = """
SELECT currency,
       count(*) AS cases,
       count(*) FILTER (WHERE state NOT IN ('recovered', 'closed')) AS open_cases,
       coalesce(sum(amount_minor) FILTER (WHERE state NOT IN ('recovered', 'closed')), 0)
           AS open_amount_minor,
       count(*) FILTER (WHERE state = 'recovered') AS recovered,
       count(*) FILTER (WHERE state = 'closed') AS closed,
       coalesce(sum(amount_minor) FILTER (WHERE state = 'recovered'), 0) AS recovered_minor,
       coalesce(sum(amount_minor), 0) AS failed_minor,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM recovered_at - opened_at))
           FILTER (WHERE state = 'recovered') / 3600.0 AS ttr_p50_hours,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM recovered_at - opened_at))
           FILTER (WHERE state = 'recovered') / 3600.0 AS ttr_p90_hours,
       min(opened_at) AS first_opened_at,
       max(opened_at) AS last_opened_at
FROM dunning_cases GROUP BY currency ORDER BY currency
"""

DAILY_SQL = """
WITH opened AS (
    SELECT opened_at::date AS day, count(*) AS opened, sum(amount_minor) AS failed_minor
    FROM dunning_cases GROUP BY 1
), recovered AS (
    SELECT recovered_at::date AS day, count(*) AS recovered, sum(amount_minor) AS recovered_minor
    FROM dunning_cases WHERE recovered_at IS NOT NULL GROUP BY 1
), days AS (
    SELECT day FROM opened UNION SELECT day FROM recovered
)
SELECT d.day, coalesce(o.opened, 0) AS opened, coalesce(o.failed_minor, 0) AS failed_minor,
       coalesce(r.recovered, 0) AS recovered, coalesce(r.recovered_minor, 0) AS recovered_minor
FROM days d LEFT JOIN opened o USING (day) LEFT JOIN recovered r USING (day)
ORDER BY d.day DESC LIMIT :n
"""

OPEN_CASES_SQL = """
SELECT c.invoice_id, c.customer_id, c.state, c.amount_minor, c.currency, c.first_reason,
       c.opened_at, c.attempts_seen, c.last_failure_at,
       d.action, d.retry_at, d.p_next_success, d.policy_kind, d.policy_version,
       d.model_version, d.fallback_reason, d.stage,
       (SELECT min(run_at) FROM retry_jobs j
         WHERE j.invoice_id = c.invoice_id AND j.state = 'scheduled') AS next_retry_at
FROM dunning_cases c
LEFT JOIN LATERAL (
    SELECT * FROM dunning_decisions x WHERE x.invoice_id = c.invoice_id
    ORDER BY x.decided_at DESC, x.recorded_at DESC LIMIT 1
) d ON true
WHERE c.state IN ('past_due', 'grace', 'restricted', 'suspended')
ORDER BY c.amount_minor DESC, c.invoice_id
LIMIT :n
"""


def _summary(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(conn.execute(text(SUMMARY_SQL)))
        states = rows(
            conn.execute(
                text(
                    "SELECT state, count(*) AS n, sum(amount_minor) AS amount_minor, currency "
                    "FROM dunning_cases GROUP BY state, currency ORDER BY state"
                )
            )
        )
        reasons = rows(
            conn.execute(
                text(
                    "SELECT coalesce(first_reason, 'unknown') AS reason, count(*) AS n "
                    "FROM dunning_cases GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
                )
            )
        )
    for r in found:
        resolved = r["recovered"] + r["closed"]
        r["resolved_recovery_rate"] = r["recovered"] / resolved if resolved else None
        r["rate_reason"] = None if resolved else "no case has been resolved yet"
    return {"by_currency": found, "states": states, "failure_reasons": reasons} if found else None


def _daily(ctx: ConsoleContext, days: int = 56) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(conn.execute(text(DAILY_SQL), {"n": days}))
    return {"days": list(reversed(found))} if found else None


def _open_cases(ctx: ConsoleContext, limit: int = 25) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(conn.execute(text(OPEN_CASES_SQL), {"n": limit}))
    return {"cases": found} if found else None


def _retries(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        states = rows(
            conn.execute(text("SELECT state, count(*) AS n FROM retry_jobs GROUP BY 1 ORDER BY 1"))
        )
        upcoming = rows(
            conn.execute(
                text(
                    "SELECT job_id, invoice_id, attempt_number, run_at, enqueued_at, dispatches "
                    "FROM retry_jobs WHERE state = 'scheduled' ORDER BY run_at, job_id LIMIT 20"
                )
            )
        )
    return {"states": states, "upcoming": upcoming} if states else None


def _policies(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(
            conn.execute(
                text(
                    "SELECT policy_kind, policy_version, model_version, count(*) AS decisions, "
                    "count(*) FILTER (WHERE fallback_reason IS NOT NULL) AS fallbacks, "
                    "avg(p_next_success) AS mean_p_next_success, "
                    "max(decided_at) AS last_decided_at "
                    "FROM dunning_decisions GROUP BY 1, 2, 3 ORDER BY max(decided_at) DESC"
                )
            )
        )
    return {"policies": found} if found else None


def _invoices(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(
            conn.execute(
                text(
                    "SELECT state, currency, count(*) AS n, sum(amount_minor) AS amount_minor "
                    "FROM invoices GROUP BY 1, 2 ORDER BY 1, 2"
                )
            )
        )
    return {"states": found} if found else None


def payments_view(ctx: ConsoleContext) -> Data:
    no_cases = (
        "no failed payment has opened a dunning case (the dunning service opens one per "
        "failed invoice; it runs in the Stripe Sandbox flow, `make stripe-verify`, not in "
        "`python -m praxis.streaming run-local`)"
    )
    return {
        "view": "payments",
        "generated_at": ctx.clock().isoformat(),
        "data_origin": "synthetic",
        "note": f"{SYNTHETIC_NOTE} Times are simulated event times.",
        "panels": {
            "summary": panel("postgres: dunning_cases", lambda: _summary(ctx), empty=no_cases),
            "daily": panel("postgres: dunning_cases", lambda: _daily(ctx), empty=no_cases),
            "open_cases": panel(
                "postgres: dunning_cases, dunning_decisions, retry_jobs",
                lambda: _open_cases(ctx),
                empty="no dunning case is open",
            ),
            "retries": panel(
                "postgres: retry_jobs", lambda: _retries(ctx), empty="no retry has been scheduled"
            ),
            "policies": panel(
                "postgres: dunning_decisions",
                lambda: _policies(ctx),
                empty="no retry decision has been recorded",
            ),
            "invoices": panel(
                "postgres: invoices", lambda: _invoices(ctx), empty="no invoice has been recorded"
            ),
        },
    }
