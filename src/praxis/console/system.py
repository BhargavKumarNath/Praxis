"""System view: event lag, API latency and errors, queue and DLQ backlog, source health, cost.

Every metric comes from ONE fleet read (``ops.system.SystemReader``: unexpired process
snapshots plus the pull collectors, ADR 0020) and is returned with that read's inputs, so the
console can mark a stale process or an unavailable collector next to the number it affects.
A metric nobody feeds is ``None`` with the feed that should supply it, never 0.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text

from praxis.agent.tools import FEEDS
from praxis.console.context import ConsoleContext
from praxis.console.cost import load_cost_estimate
from praxis.console.panels import SYNTHETIC_NOTE, Data, figure, jsonable, panel, rows
from praxis.data.config import load_sources_config
from praxis.data.freshness import check_freshness
from praxis.data.warehouse import Warehouse
from praxis.ops.health import build_health
from praxis.ops.incidents import PostgresIncidentStore
from praxis.ops.system import SystemSnapshot
from praxis.telemetry import MetricsView

# Who publishes the in-process metrics (ADR 0020 table); the pulled ones are in agent FEEDS.
PUBLISHERS = {
    **FEEDS,
    "http_request_duration_ms": "published every 15 s by the API and the console processes",
    "http_requests_total": "published every 15 s by the API and the console processes",
    "event_lag_ms": (
        "published by the event consumers (`python -m praxis.streaming consume`) and the "
        "run-local harness"
    ),
    "events_consumed_total": "published by the event consumers and the run-local harness",
}


def _missing(metric: str, system: SystemSnapshot) -> str:
    stale = sorted({p.component for p in system.processes if p.stale})
    tail = f" Stale (excluded) processes: {', '.join(stale)}." if stale else ""
    return f"No process has published {metric} ({PUBLISHERS[metric]}).{tail}"


def _error_rate(view: MetricsView, system: SystemSnapshot) -> Data:
    if not view.select("http_requests_total"):
        return figure(None, unit="ratio", reason=_missing("http_requests_total", system))
    requests = view.total("http_requests_total")
    errors = view.total("http_server_errors_total")
    return figure(errors / requests if requests else 0.0, unit="ratio")


def _by_label(view: MetricsView, metric: str, label: str) -> list[Data]:
    out: dict[str, float] = {}
    for p in view.select(metric):
        key = p.labels.get(label, "unlabelled")
        out[key] = max(out.get(key, 0.0), p.value)
    return [{label: k, "value": v} for k, v in sorted(out.items())]


def signals(system: SystemSnapshot) -> Data:
    view = system.view
    lag = view.quantile("event_lag_ms", 0.95)
    latency = view.quantile("http_request_duration_ms", 0.95)
    backlog = view.peak("queue_backlog")
    dlq = view.peak("dlq_backlog")
    if dlq is None and system.collectors.get("dead_letters", {}).get("status") == "ok":
        dlq = 0.0  # the collector read the table and found no open dead letter: a measured 0
    return {
        "event_lag_p95_ms": figure(lag, unit="ms", reason=_missing("event_lag_ms", system)),
        "api_latency_p95_ms": figure(
            latency, unit="ms", reason=_missing("http_request_duration_ms", system)
        ),
        "api_error_rate": _error_rate(view, system),
        "requests_total": figure(
            view.total("http_requests_total") if view.select("http_requests_total") else None,
            unit="count",
            reason=_missing("http_requests_total", system),
        ),
        "queue_backlog_max": figure(
            backlog, unit="messages", reason=_missing("queue_backlog", system)
        ),
        "dlq_backlog_max": figure(dlq, unit="messages", reason=_missing("dlq_backlog", system)),
        "dlq_by_subscription": _by_label(view, "dlq_backlog", "subscription"),
        "consumer_outcomes": _by_label(view, "events_consumed_total", "outcome"),
    }


def _dead_letters(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(
            conn.execute(
                text(
                    "SELECT source_subscription AS subscription, "
                    "count(*) FILTER (WHERE redriven_at IS NULL) AS open, "
                    "count(*) FILTER (WHERE redriven_at IS NOT NULL) AS redriven, "
                    "min(first_seen_at) FILTER (WHERE redriven_at IS NULL) AS oldest_open_at, "
                    "array_agg(DISTINCT reason) AS reasons "
                    "FROM dead_letters GROUP BY 1 ORDER BY 1"
                )
            )
        )
    return {"subscriptions": found} if found else None


def _batches(wh: Warehouse) -> dict[str, Data]:
    """The newest ingest batch per external source (provenance; never the endpoint URL,
    which can carry an API key in its query string)."""
    result = wh.con.execute(
        "SELECT source, batch_id, series_id, retrieved_at, source_timestamp_max, "
        "schema_version, quality_status, is_synthetic FROM raw.ingest_batches "
        "QUALIFY row_number() OVER (PARTITION BY source ORDER BY retrieved_at DESC) = 1"
    )
    cols = [d[0] for d in result.description]
    return {r[0]: jsonable(dict(zip(cols, r, strict=True))) for r in result.fetchall()}


def _sources(ctx: ConsoleContext, now: datetime) -> Data | None:
    with Warehouse(ctx.warehouse(), read_only=True) as wh:
        results = check_freshness(wh, load_sources_config(), now)
        batches = _batches(wh)
    out = []
    for r in results:
        latest = r.latest_observation
        out.append(
            {
                "source": r.source.value,
                "state": r.state.value,
                "max_age_hours": r.max_age.total_seconds() / 3600,
                "latest_observation": latest.isoformat() if latest else None,
                "age_s": max((now - latest).total_seconds(), 0.0) if latest else None,
                "latest_batch": batches.get(r.source.value),
                "provenance": "external",
            }
        )
    return {"sources": out}


def _health(ctx: ConsoleContext, system: SystemSnapshot, now: datetime) -> dict[str, Any]:
    incidents = PostgresIncidentStore(ctx.engine).live() if ctx.engine is not None else []
    report = build_health(ctx.ops, system.view, incidents, now=now, inputs=system.inputs())
    return report.as_dict()


def system_view(ctx: ConsoleContext) -> Data:
    now = ctx.clock()
    system = ctx.reader.read(now)
    health = panel(
        "fleet: health report (ops_metric_snapshots + collectors)",
        lambda: _health(ctx, system, now),
        empty="no health report",
    )
    return {
        "view": "system",
        "generated_at": now.isoformat(),
        "data_origin": "synthetic",
        "note": SYNTHETIC_NOTE,
        "inputs": system.inputs(),
        "panels": {
            "health": health,
            "signals": panel(
                "fleet: ops_metric_snapshots + collectors", lambda: signals(system), empty=""
            ),
            "dead_letters": panel(
                "postgres: dead_letters",
                lambda: _dead_letters(ctx),
                empty="no message has been dead-lettered",
            ),
            "sources": panel(
                "duckdb: raw.external_signals, raw.ingest_batches",
                lambda: _sources(ctx, now),
                empty="no external source configured",
                synthetic=False,
            ),
            "cost": panel(
                "config: configs/console/cost.toml",
                lambda: load_cost_estimate().as_dict(),
                empty="no cost estimate",
                synthetic=False,
            ),
        },
    }
