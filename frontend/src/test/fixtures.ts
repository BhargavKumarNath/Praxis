// Test-only payloads shaped like the console API's responses (typed against lib/types, so a
// contract change breaks the build here). Never imported by app code: hygiene.test.ts.

import type {
  ActionRequest,
  Candidate,
  CurrencySummary,
  DecisionDetail,
  ExecutiveBody,
  ExecutiveFigure,
  MlopsBody,
  PricingBody,
  Figure,
  IncidentDetail,
  OperatorBody,
  Panel,
  SystemBody,
} from "../lib/types";

export const NOW = "2026-10-27T12:00:00+00:00";

export function ok<T>(data: T, source = "postgres: test", synthetic = true): Panel<T> {
  return { status: "ok", source, synthetic, reason: null, data };
}

export function noData<T>(reason: string, source = "postgres: test"): Panel<T> {
  return { status: "no_data", source, synthetic: true, reason, data: null };
}

export function fig(value: number | null, reason: string | null = null): Figure {
  return { value, unit: "ms", reason, baseline: null, baseline_label: null, series: [] };
}

export function systemBody(): SystemBody {
  return {
    view: "system",
    generated_at: NOW,
    data_origin: "synthetic",
    note: "All revenue, customer, payment and recovery figures are SYNTHETIC simulator output.",
    inputs: {
      telemetry_scope: "fleet",
      processes: [
        { component: "api", instance: "h:1", published_at: NOW, expires_at: NOW, age_s: 4, stale: false, points: 3 },
        { component: "consumer-old", instance: "h:2", published_at: NOW, expires_at: NOW, age_s: 7300, stale: true, points: 1 },
      ],
      stale_processes: ["consumer-old"],
      collectors: { dead_letters: { status: "ok" } },
    },
    panels: {
      health: ok({
        status: "degraded",
        generated_at: NOW,
        config_versions: { slo: "s1", alerts: "a1" },
        sections: {
          business: {
            status: "degraded",
            panels: [],
            slos: [],
            alerts: [
              {
                rule_id: "price_volatility",
                severity: "ticket",
                summary: "More than half of the products changed price in one cycle",
                runbook: "docs/runbooks/business-kpi-shock.md",
                observed: 0.8,
                threshold: 0.5,
              },
            ],
          },
        },
        incidents: [],
      }),
      signals: ok({
        event_lag_p95_ms: fig(250),
        api_latency_p95_ms: fig(null, "No process has published http_request_duration_ms (published every 15 s by the API)."),
        api_error_rate: fig(0),
        requests_total: fig(10),
        queue_backlog_max: fig(null, "No process has published queue_backlog (the Pub/Sub emulator exposes no backlog)."),
        dlq_backlog_max: fig(0),
        dlq_by_subscription: [],
        consumer_outcomes: [],
      }, "fleet"),
      dead_letters: noData("no message has been dead-lettered"),
      sources: ok(
        {
          sources: [
            {
              source: "fred",
              state: "stale",
              max_age_hours: 1800,
              latest_observation: "2026-01-01",
              age_s: 11520,
              provenance: "external",
              latest_batch: {
                batch_id: "b1",
                series_id: "DCOILBRENTEU",
                retrieved_at: NOW,
                quality_status: "passed",
                schema_version: 1,
                is_synthetic: false,
              },
            },
            {
              source: "eia",
              state: "missing",
              max_age_hours: 120,
              latest_observation: null,
              age_s: null,
              provenance: "external",
              latest_batch: null,
            },
          ],
        },
        "duckdb: raw.external_signals",
        false,
      ),
      cost: ok(
        {
          kind: "estimate",
          label: "ESTIMATE, not a metered cost: no billing data is read",
          version: "v1",
          as_of: "2026-10-27",
          currency: "GBP",
          monthly_estimate_gbp: 0,
          not_estimated: ["Cost if the Terraform stack were applied"],
          budget_note: "Target about GBP 0 to 5 per month.",
          sources: ["docs/cost-guard.md"],
          items: [
            { resource: "Postgres", state: "local", monthly_gbp: 0, basis: "local" },
            { resource: "Cost if the Terraform stack were applied", state: "not estimated", monthly_gbp: null, basis: "none" },
          ],
        },
        "config: configs/console/cost.toml",
        false,
      ),
    },
  };
}

export function pendingRequest(overrides: Partial<ActionRequest> = {}): ActionRequest {
  return {
    request_id: "req_1",
    kind: "freeze_pricing",
    target: "*",
    params: { scope: "*" },
    rationale: "price_volatility found by deterministic triage",
    evidence: ["call_1"],
    requested_by: "praxis-agent",
    session_id: "ses_1",
    created_at: NOW,
    status: "pending",
    decided_by: null,
    decided_at: null,
    outcome: null,
    ...overrides,
  };
}

export function operatorBody(requests: ActionRequest[] = [pendingRequest()]): OperatorBody {
  return {
    view: "operator",
    generated_at: NOW,
    data_origin: "synthetic",
    note: "The investigator is deterministic triage: a rule-based program over structured tool fields, not an LLM. All figures are SYNTHETIC.",
    panels: {
      provider: ok(
        {
          policy_version: "p1",
          actor: "praxis-agent",
          enabled: true,
          provider: "deterministic-triage",
          fallback: null,
          is_llm: false,
          max_tier: 2,
          max_actions: 1,
          note: "not an LLM",
        },
        "config: configs/agent/policy.toml",
        false,
      ),
      incidents: noData("no incident has been opened"),
      requests: ok({ requests }),
      freezes: noData("no price freeze has ever been active"),
      sessions: noData("no investigation has run"),
    },
  };
}

export function incidentDetail(): IncidentDetail {
  return {
    incident: {
      incident_id: "inc_1",
      rule_id: "price_volatility",
      category: "business",
      severity: "ticket",
      status: "open",
      summary: "More than half of the products changed price in one cycle",
      runbook: "docs/runbooks/business-kpi-shock.md",
      synthetic: true,
      occurrences: 1,
      opened_at: NOW,
      updated_at: NOW,
      resolved_at: null,
      acknowledged_by: null,
      context: {},
    },
    events: [{ kind: "opened", at: NOW, detail: {} }],
    investigations: [
      {
        session: {
          session_id: "ses_1",
          started_at: NOW,
          ended_at: NOW,
          task: "investigate_incident",
          outcome: "completed",
          diagnosis: "price_volatility",
          grounded: true,
          calls: 2,
          actions: 1,
        },
        session_events: [],
        calls: [
          { call_id: "call_1", tool: "get_system_health", tier: 0, received_at: NOW, outcome_at: NOW, status: "ok", args: {}, error: null },
          { call_id: "call_2", tool: "request_price_freeze", tier: 2, received_at: NOW, outcome_at: NOW, status: "ok", args: { scope: "*" }, error: null },
        ],
        requests: [pendingRequest()],
        note: "deterministic triage, not an LLM",
      },
    ],
    note: "deterministic triage, not an LLM",
  };
}

/** A fetch double answering by path; unmatched paths fail the test loudly. */
export function fetchStub(
  routes: Record<string, (init?: RequestInit) => { status?: number; body: unknown; headers?: Record<string, string> }>,
) {
  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const key = Object.keys(routes).find((k) => url.endsWith(k));
    if (!key) throw new Error(`unexpected fetch ${url}`);
    const r = routes[key]!(init);
    return new Response(JSON.stringify(r.body), {
      status: r.status ?? 200,
      headers: { "Content-Type": "application/json", "X-Correlation-ID": "cid-header", ...(r.headers ?? {}) },
    });
  };
}

// ---- pricing / mlops / payments / executive ----------------------------------------------


function candidate(price: number, feasible: boolean): Candidate {
  return {
    price_micros: price, log_ratio: 0, expected_demand: 1, contribution: 1, churn_cost: 0,
    delta_mean: (368_000 - price) * 1000, delta_sd: 1, delta_p05: -1e6, delta_p95: 1e6,
    delta_contribution_mean: 0, prob_improvement: 0.5, risk_adjusted: 0,
    feasible, violations: feasible ? [] : ["capacity"],
  };
}

export function decisionDetail(): DecisionDetail {
  return {
    decision: {
      decision_id: "dec-1", cycle_id: "c1", product: "api_requests", as_of: "2026-09-07",
      mode: "shadow", policy_version: "policy-abc", status: "hold",
      current_price_micros: 368_000, chosen_price_micros: null, reason_codes: ["no_improvement"],
      lineage: { forecast_model_version: "demand-hybrid-24c8", elasticity_model_version: "elasticity-hier-0ff4", market_snapshot: "market-68" },
      inputs: {},
      constraints: { max_step: 0.05, price_floor_micros: 200_000 },
      candidates: [candidate(350_500, false), candidate(368_000, true), candidate(380_000, true)],
      rejected_alternatives: [{ price_micros: 350_500, reasons: ["capacity"] }],
      guardrails: { passed: false, checks: { capacity: true, margin_floor: false } },
      prediction: null, errors: [], is_synthetic: true,
    },
    record_sha256: "a".repeat(64),
    trace_id: "trace-1",
    recorded_at: NOW,
    execution: null,
    shadow: true,
    note: "shadow: not validated for execution (Phase 4 loader defect). SYNTHETIC.",
  };
}

export function mlopsBody(): MlopsBody {
  const version = {
    model_version: "demand-hybrid-24c8", model_name: "demand_forecaster", stage: "champion",
    data_version: "panel-1", feature_version: "demand_features.v1", code_revision: "0cb9304",
    artifact_checksum: "c".repeat(64),
    metrics: { backtest: { hybrid: { vwape: 0.135, coverage_80: 0.79 } } },
    trained_at: NOW, registered_at: NOW, stage_changed_at: NOW, is_synthetic: true,
    validation: { passed: true, failed_checks: [], created_at: NOW },
  };
  const event = (from: string | null, to: string, version_: string) => ({
    decision_id: "promote-1", model_name: "demand_forecaster", model_version: version_,
    from_stage: from, to_stage: to, trigger: "promote.forced", actor: "console-demo",
    reason: "demo", occurred_at: NOW,
  });
  return {
    view: "mlops", generated_at: NOW, data_origin: "synthetic", note: "SYNTHETIC data.",
    panels: {
      versions: ok({ versions: [version, { ...version, model_version: "demand-hybrid-old", stage: "retired", validation: null }], champions: { demand_forecaster: "demand-hybrid-24c8" } }),
      // one promotion decision writes two stage events (old champion out, new one in)
      events: ok({ events: [event("challenger", "champion", "demand-hybrid-24c8"), event("champion", "retired", "demand-hybrid-old")] }),
      monitoring: noData("no drift, shadow or probation report has been written"),
      retraining: noData("no retraining has been requested"),
      serving: ok({
        calibration_80: fig(null, "No process has published model_calibration_80."),
        drift_alarm: fig(0), forecast_age_hours: fig(24), oldest_source_age_s: fig(null, "no source"),
        serving_versions: [], inputs: { telemetry_scope: "fleet" },
      }, "fleet"),
    },
  };
}

export function currency(overrides: Partial<CurrencySummary> = {}): CurrencySummary {
  return {
    currency: "GBP", cases: 42, open_cases: 28, open_amount_minor: 137_200, recovered: 14,
    closed: 0, recovered_minor: 68_600, failed_minor: 205_800, ttr_p50_hours: 72,
    ttr_p90_hours: 72, first_opened_at: NOW, last_opened_at: NOW,
    resolved_recovery_rate: 1, rate_reason: null, ...overrides,
  };
}

function exec(value: number | null, latest: string, reason: string | null = null): ExecutiveFigure {
  return {
    figure: { value, unit: "x", reason, baseline: value === null ? null : 1, baseline_label: "median of the previous 28 days", series: [] },
    latest_day: latest, baseline_days: 28, definition: "definition",
  };
}

export function executiveBody(): ExecutiveBody {
  const p = (f: ExecutiveFigure) => ok(f, "duckdb: marts");
  return {
    view: "executive", generated_at: NOW, data_origin: "synthetic", note: "SYNTHETIC.", window_days: 56,
    panels: {
      revenue: p(exec(9_846_929, "2026-09-13")),
      gross_margin: { status: "unavailable", source: "duckdb: marts.fct_marginal_cost_daily", synthetic: true, reason: "a warehouse table this panel needs is not built (run `make dbt-build`)", data: null },
      demand: p(exec(530_794, "2026-09-13")),
      active_customers: p(exec(659, "2026-09-13")),
      churn: p(exec(1, "2026-09-13")),
      recovery_rate: p(exec(0, "2026-09-13")),
      price_index: p(exec(105, "2026-07-18")),
    },
  };
}

export function pricingBody(): PricingBody {
  return {
    view: "pricing", generated_at: NOW, data_origin: "synthetic", shadow: true,
    note: "shadow: not validated for execution (Phase 4 loader defect). SYNTHETIC.",
    panels: {
      latest: ok({
        products: [{
          decision_id: "dec-1", cycle_id: "c1", product: "api_requests", as_of: "2026-09-07",
          mode: "shadow", status: "hold", current_price_micros: 368_000, chosen_price_micros: null,
          policy_version: "policy-abc", reason_codes: ["no_improvement"], recorded_at: NOW, trace_id: null,
          prediction: {
            price_micros: 368_000, delta_objective_mean: 0, delta_objective_sd: 0, delta_objective_p05: 0,
            delta_objective_p95: 0, delta_contribution_mean: 0, churn_cost: 0, prob_improvement: 0,
            expected_demand: 55_848,
          },
          elasticity: { growth: { mean: -1.185, sd: 0.065 } },
          forecast_relative_width: 0.37, lineage: null, guardrails_passed: true,
          executed_at: null, executed_by: null,
        }],
      }),
      history: noData("no decision has been recorded"),
      recent: noData("no decision has been recorded"),
      status_counts: noData("no decision has been recorded"),
    },
  };
}
