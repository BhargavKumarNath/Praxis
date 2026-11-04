"""Spend guard (deployment, ADR 0026): budget notification -> unlink billing at the ceiling.

Google Cloud budgets only notify. The budget publishes its state to Pub/Sub several times a
day; a push subscription (OIDC-authenticated, private Cloud Run service) delivers it here.
When the named budget's actual cost reaches ``spend_guard_kill_at_gbp`` the guard removes the
billing account from the project, which stops every billable resource at once (Google's
documented pattern; resources may be deleted, accepted for this synthetic environment).

Pub/Sub semantics: a 2xx acks. A message that can never be acted on (malformed, another
budget, another currency) is acked with its reason, so it is not retried forever; a failed
billing call answers 503 so Pub/Sub redelivers with backoff. Amounts are ``Decimal``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.auth.exceptions import GoogleAuthError
from requests import RequestException

from praxis.api.middleware import docs_routes, install_middleware
from praxis.config import Settings, get_settings
from praxis.errors import TransientError
from praxis.logging import configure_logging

logger = logging.getLogger(__name__)
BILLING_API = "https://cloudbilling.googleapis.com/v1"
Action = Literal["ignore", "hold", "disable"]


class BillingControl(Protocol):
    def billing_enabled(self, project_id: str) -> bool: ...

    def disable_billing(self, project_id: str) -> None: ...


@dataclass(frozen=True)
class BudgetState:
    budget_name: str
    cost: Decimal
    budget: Decimal
    currency: str


@dataclass(frozen=True)
class Verdict:
    action: Action
    reason: str


def _amount(raw: Any, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a number") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a finite, non-negative amount")
    return value


def parse_push(body: Any) -> BudgetState:
    """A Pub/Sub push envelope carrying a budget notification (schema version 1.0)."""
    if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
        raise ValueError("not a Pub/Sub push envelope")
    try:
        data = json.loads(base64.b64decode(body["message"].get("data", ""), validate=True))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("message data is not base64 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("notification is not an object")
    name, currency = data.get("budgetDisplayName"), data.get("currencyCode")
    if not isinstance(name, str) or not isinstance(currency, str):
        raise ValueError("budgetDisplayName and currencyCode are required")
    return BudgetState(
        budget_name=name,
        cost=_amount(data.get("costAmount"), "costAmount"),
        budget=_amount(data.get("budgetAmount"), "budgetAmount"),
        currency=currency,
    )


def decide(state: BudgetState, *, budget_name: str | None, kill_at: Decimal | None) -> Verdict:
    if budget_name is None or kill_at is None:
        return Verdict("ignore", "guard not configured (report only)")
    if state.budget_name != budget_name:
        return Verdict("ignore", f"notification is for budget {state.budget_name!r}")
    if state.currency != "GBP":
        return Verdict("ignore", f"currency {state.currency} is not GBP")
    if state.cost >= kill_at:
        return Verdict("disable", f"cost GBP {state.cost} >= guard GBP {kill_at}")
    return Verdict("hold", f"cost GBP {state.cost} < guard GBP {kill_at}")


class CloudBilling:
    """Cloud Billing API with the service's own identity (roles/billing.projectManager)."""

    def __init__(self, session: Any | None = None, *, timeout_s: float = 10.0) -> None:
        self._session = session
        self._timeout = timeout_s

    def _http(self) -> Any:
        if self._session is None:  # lazily: no network or credential lookup at import / start
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-billing"]
            )
            self._session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]
        return self._session

    def _call(self, method: str, project_id: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{BILLING_API}/projects/{project_id}/billingInfo"
        try:
            response = self._http().request(method, url, json=body, timeout=self._timeout)
        except (RequestException, GoogleAuthError) as exc:
            raise TransientError(f"billing API unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise TransientError(f"billing API answered {response.status_code}")
        return response.json()

    def billing_enabled(self, project_id: str) -> bool:
        return bool(self._call("GET", project_id).get("billingEnabled"))

    def disable_billing(self, project_id: str) -> None:
        self._call("PUT", project_id, {"billingAccountName": ""})


def create_guard_app(
    settings: Settings | None = None, billing: BillingControl | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level.value)
    control: BillingControl = billing or CloudBilling()
    project = settings.gcp_project_id
    docs = docs_routes(settings)
    app = FastAPI(
        title="Praxis spend guard",
        docs_url=docs.docs_url, redoc_url=docs.redoc_url, openapi_url=docs.openapi_url,
    )  # fmt: skip
    install_middleware(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": settings.service_name}

    @app.post("/v1/budget")
    async def budget(request: Request) -> JSONResponse:
        try:
            state = parse_push(await request.json())
        except ValueError as exc:
            logger.error("budget notification rejected", extra={"reason": str(exc)})
            return JSONResponse({"action": "ignore", "reason": str(exc)})
        verdict = decide(
            state,
            budget_name=settings.spend_guard_budget_name,
            kill_at=settings.spend_guard_kill_at_gbp,
        )
        log = {"action": verdict.action, "reason": verdict.reason, "cost_gbp": str(state.cost),
               "budget_gbp": str(state.budget), "budget_name": state.budget_name}  # fmt: skip
        if verdict.action != "disable" or project is None:
            logger.info("budget notification", extra=log)
            return JSONResponse({"action": verdict.action, "reason": verdict.reason})
        try:
            if control.billing_enabled(project):
                control.disable_billing(project)
                logger.critical("spend guard unlinked billing", extra=log | {"project": project})
            else:
                logger.warning("spend guard: billing already disabled", extra=log)
        except TransientError as exc:
            logger.error("spend guard could not unlink billing", extra=log | {"error": str(exc)})
            return JSONResponse({"action": "retry", "reason": str(exc)}, status_code=503)
        return JSONResponse({"action": "disable", "reason": verdict.reason})

    return app
