"""The decision console's HTTP API (ADR 0021): one read endpoint per dashboard view, detail
endpoints, and three guarded admin actions.

* Reads (``GET /v1/console/...``) are open on the local network interface the process binds
  to; they return panels that say where every number came from (``console.panels``).
* Admin actions need ``Authorization: Bearer <token>`` of a named approver
  (``PRAXIS_CONSOLE_APPROVERS``); the token decides who approves. They run ONLY the existing
  paths: ``ApprovalDesk.approve / reject`` (which re-check their own policy, e.g.
  ``ModelRegistry.promote`` re-checks shadow readiness) and ``FreezeStore.lift``. No route
  accepts a price, and the console cannot reach the optimiser or payment execution (import
  contract).
* Errors carry the correlation id and a fixed message, never exception text or a trace.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Header, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from praxis import __version__
from praxis.agent.config import load_agent_policy
from praxis.agent.requests import ApprovalDesk, PostgresRequestStore, RequestError
from praxis.api.middleware import install_middleware, publisher_lifespan
from praxis.config import Settings, get_settings
from praxis.console.auth import Approvers, ApproversConfigError, bearer, parse_approvers
from praxis.console.context import ConsoleContext
from praxis.console.executive import executive_view
from praxis.console.mlops import mlops_view
from praxis.console.operator import incident_detail, operator_view, session_trace
from praxis.console.panels import NotConfigured
from praxis.console.payments import payments_view
from praxis.console.pricing import decision_detail, pricing_view
from praxis.console.system import system_view
from praxis.control.db import make_engine
from praxis.control.metric_snapshots import MetricSnapshotStore, SnapshotPublisher
from praxis.logging import configure_logging
from praxis.mlops.config import load_mlops_config
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.store import RegistryStore
from praxis.ops.collect import STORE_ERRORS
from praxis.ops.system import SystemReader, build_reader
from praxis.pricing.freeze import FreezeError, FreezeStore, PostgresFreezeStore
from praxis.tracing import current_correlation_id

logger = logging.getLogger(__name__)

Ident = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class ConsoleError(BaseModel):
    error: str
    correlation_id: str | None
    detail: str | None = None


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


def _error(status: int, error: str, detail: str | None = None) -> JSONResponse:
    body = ConsoleError(error=error, correlation_id=current_correlation_id(), detail=detail)
    return JSONResponse(status_code=status, content=body.model_dump())


def _guarded(read: Callable[[], Any], missing: str) -> Any:
    """A detail read: 404 when absent, 503 (fixed text) when the store does not answer."""
    try:
        found = read()
    except NotConfigured:
        return _error(503, "not_configured", "the control-plane database is not configured")
    except STORE_ERRORS:
        logger.warning("console.store_unavailable", exc_info=True)
        return _error(503, "store_unavailable", "the store did not answer; retry shortly")
    return found if found is not None else _error(404, "not_found", missing)


ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ConsoleError},
    404: {"model": ConsoleError},
    409: {"model": ConsoleError},
    503: {"model": ConsoleError},
}


class AdminDeps:
    def __init__(
        self, approvers: Approvers, desk: ApprovalDesk | None, freezes: FreezeStore | None
    ) -> None:
        self.approvers = approvers
        self.desk = desk
        self.freezes = freezes

    def approver(self, authorization: str | None) -> str | JSONResponse:
        """The named approver for this request, or the refusal to send back."""
        if not self.approvers.configured or self.desk is None or self.freezes is None:
            return _error(503, "admin_not_configured", "no approvers are configured")
        token = bearer(authorization)
        name = self.approvers.identify(token) if token else None
        if name is None:
            logger.warning("console.admin_refused", extra={"reason": "invalid_token"})
            response = _error(401, "unauthorized", "a valid approver token is required")
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
        return name


def _decide(
    deps: AdminDeps, request_id: str, approver: str, action: str, reason: str | None
) -> Any:
    desk = deps.desk
    assert desk is not None  # noqa: S101 - checked by AdminDeps.approver
    if desk.store.get(request_id) is None:
        return _error(404, "not_found", f"no request {request_id}")
    try:
        if action == "approve":
            done = desk.approve(request_id, approver)
        else:
            done = desk.reject(request_id, approver, reason or "")
    except RequestError as refused:  # our own messages: not pending, requester, ...
        return _error(409, "refused", str(refused))
    logger.info(
        "console.admin_action",
        extra={
            "action": action,
            "request_id": request_id,
            "approver": approver,
            "status": done.status,
        },
    )
    return {"request": done.as_dict()}


def admin_router(deps: AdminDeps) -> APIRouter:
    router = APIRouter(prefix="/v1/console", tags=["console-admin"])

    @router.post("/requests/{request_id}/approve", responses=ERRORS)
    def approve(request_id: Ident, authorization: Annotated[str | None, Header()] = None) -> Any:
        who = deps.approver(authorization)
        if isinstance(who, JSONResponse):
            return who
        return _guarded(lambda: _decide(deps, request_id, who, "approve", None), "")

    @router.post("/requests/{request_id}/reject", responses=ERRORS)
    def reject(
        request_id: Ident,
        body: RejectBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Any:
        who = deps.approver(authorization)
        if isinstance(who, JSONResponse):
            return who
        return _guarded(lambda: _decide(deps, request_id, who, "reject", body.reason), "")

    @router.post("/freezes/{freeze_id}/lift", responses=ERRORS)
    def lift(freeze_id: Ident, authorization: Annotated[str | None, Header()] = None) -> Any:
        who = deps.approver(authorization)
        if isinstance(who, JSONResponse):
            return who
        assert deps.freezes is not None  # noqa: S101 - checked by AdminDeps.approver
        freezes = deps.freezes

        def run() -> Any:
            try:
                lifted = freezes.lift(freeze_id, by=who)
            except FreezeError:
                return _error(404, "not_found", f"no active freeze {freeze_id}")
            logger.info(
                "console.admin_action",
                extra={"action": "lift_freeze", "freeze_id": freeze_id, "approver": who},
            )
            return {"freeze": lifted.as_dict()}

        return _guarded(run, "")

    @router.get("/whoami", responses=ERRORS)
    def whoami(authorization: Annotated[str | None, Header()] = None) -> Any:
        who = deps.approver(authorization)
        return who if isinstance(who, JSONResponse) else {"approver": who}

    return router


def read_router(ctx: ConsoleContext) -> APIRouter:
    router = APIRouter(prefix="/v1/console", tags=["console"])
    views: dict[str, Callable[[ConsoleContext], dict[str, Any]]] = {
        "executive": executive_view,
        "pricing": pricing_view,
        "payments": payments_view,
        "mlops": mlops_view,
        "system": system_view,
        "operator": operator_view,
    }
    for name, build in views.items():
        router.add_api_route(
            f"/{name}", _view_endpoint(ctx, build), methods=["GET"], name=f"console_{name}"
        )

    @router.get("/incidents/{incident_id}", responses=ERRORS)
    def incident(incident_id: Ident) -> Any:
        return _guarded(lambda: incident_detail(ctx, incident_id), f"no incident {incident_id}")

    @router.get("/sessions/{session_id}", responses=ERRORS)
    def session(session_id: Ident) -> Any:
        return _guarded(lambda: session_trace(ctx, session_id), f"no session {session_id}")

    @router.get("/pricing/decisions/{decision_id}", responses=ERRORS)
    def decision(decision_id: Ident) -> Any:
        return _guarded(lambda: decision_detail(ctx, decision_id), f"no decision {decision_id}")

    return router


def _view_endpoint(
    ctx: ConsoleContext, build: Callable[[ConsoleContext], dict[str, Any]]
) -> Callable[[], dict[str, Any]]:
    def endpoint() -> dict[str, Any]:
        return build(ctx)

    return endpoint


def build_approvers(settings: Settings) -> Approvers:
    raw = settings.console_approvers.get_secret_value() if settings.console_approvers else None
    approvers = parse_approvers(raw)
    actor = load_agent_policy().agent.actor
    if any(name == actor for name, _ in approvers.tokens):
        raise ApproversConfigError(f"{actor!r} is the AI operator's identity, not an approver")
    return approvers


def create_console_app(
    settings: Settings | None = None,
    *,
    context: ConsoleContext | None = None,
    approvers: Approvers | None = None,
    publisher: SnapshotPublisher | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level.value)
    engine = (
        make_engine(settings.database_url.get_secret_value()) if settings.database_url else None
    )
    if context is None:
        if engine is not None and publisher is None:
            publisher = SnapshotPublisher(MetricSnapshotStore(engine), "console")
        own = ("console", publisher.instance) if publisher is not None else None
        reader = (
            build_reader(engine, settings.warehouse_path, own=own) if engine else SystemReader()
        )
        context = ConsoleContext(engine, settings.warehouse_path, reader)
    desk, freezes = _admin_paths(context, settings)
    app = FastAPI(
        title="Praxis console", version=__version__, lifespan=publisher_lifespan(publisher)
    )
    install_middleware(app)
    app.include_router(read_router(context))
    deps = AdminDeps(
        approvers if approvers is not None else build_approvers(settings), desk, freezes
    )
    app.include_router(admin_router(deps))
    return app


def _admin_paths(
    ctx: ConsoleContext, settings: Settings
) -> tuple[ApprovalDesk | None, FreezeStore | None]:
    if ctx.engine is None:
        return None, None
    registry = ModelRegistry(
        RegistryStore(ctx.engine), load_mlops_config(), environment=settings.environment
    )
    freezes = PostgresFreezeStore(ctx.engine)
    desk = ApprovalDesk(PostgresRequestStore(ctx.engine), registry=registry, freezes=freezes)
    return desk, freezes
