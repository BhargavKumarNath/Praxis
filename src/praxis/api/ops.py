"""Operations endpoints: health report (JSON + HTML) and incidents. Read-only.

Evaluation of alerts into incidents happens in ``OpsMonitor`` / the ops CLI, never as a side
effect of a GET. With a ``SystemReader`` the report reads the whole system (fleet snapshots,
collectors, this process; ADR 0020) and says where each input came from; without one it
reads this process's metrics only (``inputs.telemetry_scope = "process"``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from praxis.ops.config import OpsConfig
from praxis.ops.health import HealthReport, build_health, render_html
from praxis.ops.incidents import IncidentStore
from praxis.ops.system import SystemReader
from praxis.telemetry import telemetry
from praxis.tracing import current_correlation_id


def _report(
    cfg: OpsConfig, store: IncidentStore | None, reader: SystemReader | None
) -> HealthReport:
    incidents = store.live() if store is not None else []
    now = datetime.now(UTC)
    if reader is None:
        return build_health(cfg, telemetry().snapshot(), incidents, now=now)
    system = reader.read(now)
    return build_health(cfg, system.view, incidents, now=now, inputs=system.inputs())


def ops_router(
    cfg: OpsConfig, store: IncidentStore | None, reader: SystemReader | None = None
) -> APIRouter:
    router = APIRouter(tags=["ops"])

    @router.get("/v1/ops/health")
    def health() -> dict[str, Any]:
        return _report(cfg, store, reader).as_dict()

    @router.get("/ops/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return render_html(_report(cfg, store, reader))

    @router.get("/v1/ops/incidents", responses={503: {}})
    def incidents(limit: int = 50) -> Any:
        if store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "incidents_unavailable",
                    "correlation_id": current_correlation_id(),
                },
            )
        return {"incidents": [i.as_dict() for i in store.recent(max(1, min(limit, 200)))]}

    return router
