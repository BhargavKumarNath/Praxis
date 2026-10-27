# ADR 0021: Decision console, its read model and the admin boundary

Status: Accepted (2026-10-27, Phase 13). Adds the top import layer `praxis.console` and the
contract "The decision console cannot set prices, execute payments or reach ground truth".
Changes no threshold, floor or existing contract.

## Context
Phase 13 needs a dashboard that reads real system state across the control plane (Postgres),
the warehouse (DuckDB) and the fleet metrics (`SystemReader`, ADR 0020); marks stale and
missing inputs instead of showing 0; labels synthetic, external and shadow data; and lets a
named person approve or reject the AI operator's guarded requests and lift a freeze, without
ever letting the UI set a price or bypass the optimiser. Two constraints shape it:

* the core API must never load `praxis.agent` (`tests/agent/test_availability.py`), but the
  operator view must read the agent's audit and request stores;
* local only, GBP 0, few dependencies (CLAUDE.md s14, s18).

## Decision
* **A separate process, `praxis.console`,** in a new top layer above `api | science | agent`.
  It is a FastAPI app (`create_console_app`, `make console-api`, loopback port 8001) with read
  routes under `/v1/console/*` and three write routes. It shares the core API's middleware
  (`praxis.api.middleware`, extracted from `api.app`): correlation ids, safe 422 / 500 bodies,
  request telemetry, the snapshot lifespan. The core API is otherwise unchanged and still
  never imports the agent.
* **A read model of panels.** Every view returns panels of the form `{status: ok | no_data |
  unavailable, source, synthetic, reason, data}`. `no_data` and `unavailable` always carry a
  reason; a store failure becomes a fixed sentence, never exception text. A figure that is
  missing is `None` with the feed that should supply it, never 0 (`panels.figure` requires the
  reason). A measured zero stays 0: the dead-letter collector reading an empty table yields 0.
  Fleet metrics come from one `SystemReader` read per request, returned with its inputs (which
  processes are live or stale, and each collector's status). History comes from the stores of
  record, never from snapshots, which hold latest values only.
* **The admin boundary.** The only write routes are `POST /requests/{id}/approve`,
  `POST /requests/{id}/reject` (reason required) and `POST /freezes/{id}/lift`, plus
  `GET /whoami`. They call the existing paths: `ApprovalDesk` over `PostgresRequestStore`,
  `ModelRegistry` (promotion and rollback requests) and `PostgresFreezeStore`. Promotion and
  rollback are therefore possible only as approvals of requests the existing flow created;
  there is no direct promote or rollback button. Authorization is server-side:
  `PRAXIS_CONSOLE_APPROVERS="name=token,..."` (Secret Manager in a cloud deployment). The
  bearer token identifies the approver, so the client cannot name one (the request body has no
  approver field). Comparison is constant-time against every entry; names are validated;
  reserved service identities (`agent`, `system`, `ops-monitor`, `mlops-automation`) and the
  AI operator's own actor are refused; tokens are at least 24 characters and never echoed.
  Without approvers the admin routes answer 503 and the console is read-only. Bearer tokens
  in a header, never cookies, so there is no CSRF surface. No route or schema field can carry
  a price (`tests/console/test_admin.py` checks the OpenAPI document).
* **Import contract.** `praxis.console` may not import the optimiser, the pricing service or
  CLI, payment gateways, the processor, the Stripe client, the dunning executor, service,
  tasks, consumer or wiring, MLOps training or pipeline, streaming, the simulator, science,
  `praxis.api.app`, `api.tasks`, `api.webhooks`, `api.forecast` or `subprocess`.
* **The frontend** (`frontend/`, Next 16, React 19) calls only `/api/console/*`, which Next
  rewrites to the console API, so the browser has one origin and no CORS surface. Data is read
  client-side with a 30 s refresh; the last good render stays on screen during a refresh and
  after a failure, and a view older than two refresh intervals, or whose refresh failed, is
  marked STALE with its age. A failed call shows a fixed message and the correlation id.
  Charts are hand-built SVG (one axis, gaps kept as gaps, uncertainty as bands, thresholds
  from config, a table view for every chart), so there is no chart or UI library. The approver
  token lives in `sessionStorage` for the tab only.

## Alternatives considered
* **Console routes inside the core API.** Rejected: the operator view needs agent stores,
  and the core must stay agent-free.
* **Server-rendered pages reading the stores directly.** Rejected: it would put database
  credentials and store code in the Node process and duplicate the read model in TypeScript.
* **A charting library (Recharts, visx, ECharts).** Rejected under s18: the chart grammar is
  small and fixed (`docs/design/dashboard.md`), and a library adds weight and defaults that
  fight it.
* **Sessions with a login.** Deferred: a static token list is enough for a local,
  single-operator system and keeps authorization in one constant-time check. It has no expiry
  or rotation (see Consequences).

## Consequences
* Two Python processes run locally (API on 8000, console on 8001), plus Next.
* The console publishes its own metric snapshot (component `console`) like any long-running
  process (ADR 0020).
* Approver tokens are static configuration: no expiry, rotation or per-action second factor.
  A cloud deployment would put them in Secret Manager and should add rotation before real use.
* New dev dependencies, exact pins: `vitest 5.0.3`, `jsdom 30.1.2`,
  `@testing-library/react 16.3.3`, `@testing-library/dom 10.4.2`, `@playwright/test 1.64.0`.
  No new runtime dependency (fonts are self-hosted by `next/font` at build time).
* Gates: `make frontend-check` now also runs the Vitest suite; `make e2e` (CI job `e2e`) runs
  the Playwright journey on a fresh Postgres; `src/praxis/console/*` has a 95% coverage floor.
