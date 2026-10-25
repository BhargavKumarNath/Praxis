"""Operations endpoints: health report (JSON + HTML) and incidents. Read-only.

Evaluation of alerts into incidents happens in ``OpsMonitor`` / the ops CLI, never as a side
effect of a GET. The report reads this process's metrics (``praxis.telemetry``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from praxis.ops.config import OpsConfig
from praxis.ops.health import HealthReport, build_health, render_html
from praxis.ops.incidents import IncidentStore
from praxis.telemetry import telemetry
from praxis.tracing import current_correlation_id


def _report(cfg: OpsConfig, store: IncidentStore | None) -> HealthReport:
    incidents = store.live() if store is not None else []
    return build_health(cfg, telemetry().snapshot(), incidents)


def ops_router(cfg: OpsConfig, store: IncidentStore | None) -> APIRouter:
    router = APIRouter(tags=["ops"])

    @router.get("/v1/ops/health")
    def health() -> dict[str, Any]:
        return _report(cfg, store).as_dict()

    @router.get("/ops/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return render_html(_report(cfg, store))

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
