// Contracts of the console read API (src/praxis/console). Every view returns panels; a panel
// says where its data came from and, when it has none, why.

export type PanelStatus = "ok" | "no_data" | "unavailable";

export interface Panel<T> {
  status: PanelStatus;
  source: string;
  synthetic: boolean;
  reason: string | null;
  error?: string;
  data: T | null;
}

export interface ViewBody<P> {
  view: string;
  generated_at: string;
  data_origin: "synthetic";
  note: string;
  panels: P;
}

export interface SeriesPoint {
  day: string;
  value: number;
}

export interface Figure {
  value: number | null;
  unit: string;
  reason: string | null;
  baseline: number | null;
  baseline_label: string | null;
  series: SeriesPoint[];
}

export interface ApiError {
  status: number;
  error: string;
  correlationId: string | null;
  detail: string | null;
}

// ---- system -------------------------------------------------------------------------------
export interface ProcessInput {
  component: string;
  instance: string;
  published_at: string;
  expires_at: string;
  age_s: number;
  stale: boolean;
  points: number;
}

export interface Inputs {
  telemetry_scope: string;
  processes?: ProcessInput[];
  stale_processes?: string[];
  collectors?: Record<string, { status: string; error?: string; [key: string]: unknown }>;
}

export interface Alert {
  rule_id: string;
  severity: string;
  summary: string;
  runbook: string;
  observed?: number;
  threshold?: number | null;
  synthetic?: boolean;
}

export interface Slo {
  id: string;
  category: string;
  description: string;
  state: string;
  target: number;
  observed: number | null;
  budget_consumed: number | null;
}

export interface HealthSection {
  status: string;
  panels: { title: string; metric: string; agg: string; value: number | null }[];
  slos: Slo[];
  alerts: Alert[];
}

export interface Health {
  status: string;
  generated_at: string;
  config_versions: Record<string, string>;
  sections: Record<string, HealthSection>;
  incidents: Incident[];
}

export interface Signals {
  event_lag_p95_ms: Figure;
  api_latency_p95_ms: Figure;
  api_error_rate: Figure;
  requests_total: Figure;
  queue_backlog_max: Figure;
  dlq_backlog_max: Figure;
  dlq_by_subscription: { subscription: string; value: number }[];
  consumer_outcomes: { outcome: string; value: number }[];
}

export interface SourceHealth {
  source: string;
  state: "fresh" | "stale" | "missing";
  max_age_hours: number;
  latest_observation: string | null;
  age_s: number | null;
  provenance: "external";
  latest_batch: {
    batch_id: string;
    series_id: string;
    retrieved_at: string;
    quality_status: string;
    schema_version: number;
    is_synthetic: boolean;
  } | null;
}

export interface CostEstimate {
  kind: "estimate";
  label: string;
  version: string;
  as_of: string;
  currency: string;
  monthly_estimate_gbp: number;
  not_estimated: string[];
  budget_note: string;
  sources: string[];
  items: { resource: string; state: string; monthly_gbp: number | null; basis: string }[];
}

export interface SystemBody
  extends ViewBody<{
    health: Panel<Health>;
    signals: Panel<Signals>;
    dead_letters: Panel<{
      subscriptions: {
        subscription: string;
        open: number;
        redriven: number;
        oldest_open_at: string | null;
        reasons: string[];
      }[];
    }>;
    sources: Panel<{ sources: SourceHealth[] }>;
    cost: Panel<CostEstimate>;
  }> {
  inputs: Inputs;
}

// ---- operator -----------------------------------------------------------------------------
export interface Incident {
  incident_id: string;
  rule_id: string;
  category: string;
  severity: string;
  status: "open" | "acknowledged" | "resolved";
  summary: string;
  runbook: string;
  synthetic: boolean;
  occurrences: number;
  opened_at: string;
  updated_at: string;
  resolved_at: string | null;
  acknowledged_by: string | null;
  context: Record<string, unknown>;
}

export interface ActionRequest {
  request_id: string;
  kind: "freeze_pricing" | "rollback" | "promotion";
  target: string;
  params: Record<string, unknown>;
  rationale: string;
  evidence: string[];
  requested_by: string;
  session_id: string;
  created_at: string;
  status: "pending" | "executing" | "completed" | "failed" | "rejected";
  decided_by: string | null;
  decided_at: string | null;
  outcome: Record<string, unknown> | null;
}

export interface Freeze {
  freeze_id: string;
  scope: string;
  reason: string;
  requested_by: string;
  approved_by: string;
  started_at: string;
  active?: boolean;
  lifted_at: string | null;
  lifted_by: string | null;
}

export interface SessionSummary {
  session_id: string;
  started_at: string;
  ended_at: string;
  task: string | null;
  outcome: string | null;
  diagnosis: string | null;
  grounded: boolean | null;
  calls: number;
  actions: number;
}

export interface Call {
  call_id: string;
  tool: string;
  tier: number | null;
  received_at: string | null;
  outcome_at: string | null;
  status: string | null;
  args: Record<string, unknown>;
  error: string | null;
}

export interface Trace {
  session: SessionSummary;
  session_events: { status: string; at: string; detail: Record<string, unknown> }[];
  calls: Call[];
  requests: ActionRequest[];
  note: string;
}

export interface Provider {
  policy_version: string;
  actor: string;
  enabled: boolean;
  provider: string;
  fallback: string | null;
  is_llm: false;
  max_tier: number;
  max_actions: number;
  note: string;
}

export type OperatorBody = ViewBody<{
  provider: Panel<Provider>;
  incidents: Panel<{ incidents: Incident[] }>;
  requests: Panel<{ requests: ActionRequest[] }>;
  freezes: Panel<{ active: Freeze[]; lifted: Freeze[] }>;
  sessions: Panel<{ sessions: SessionSummary[] }>;
}>;

export interface IncidentDetail {
  incident: Incident;
  events: { kind: string; at: string; detail: Record<string, unknown> }[];
  investigations: Trace[];
  note: string;
}

// ---- pricing ------------------------------------------------------------------------------
export interface Prediction {
  price_micros: number;
  delta_objective_mean: number;
  delta_objective_sd: number;
  delta_objective_p05: number;
  delta_objective_p95: number;
  delta_contribution_mean: number;
  churn_cost: number;
  prob_improvement: number;
  expected_demand: number;
}

export interface LatestDecision {
  decision_id: string;
  cycle_id: string;
  product: string;
  as_of: string;
  mode: string;
  status: string;
  current_price_micros: number | null;
  chosen_price_micros: number | null;
  policy_version: string;
  reason_codes: string[];
  recorded_at: string;
  trace_id: string | null;
  prediction: Prediction | null;
  elasticity: Record<string, { mean: number; sd: number }> | null;
  forecast_relative_width: number | null;
  lineage: Record<string, string> | null;
  guardrails_passed: boolean | null;
  executed_at: string | null;
  executed_by: string | null;
}

export interface HistoryPoint {
  as_of: string;
  mode: string;
  status: string;
  current_price_micros: number | null;
  chosen_price_micros: number | null;
  decision_id: string;
  delta_mean: number | null;
  delta_p05: number | null;
  delta_p95: number | null;
  expected_demand: number | null;
}

export interface DecisionRow {
  decision_id: string;
  product: string;
  as_of: string;
  mode: string;
  status: string;
  current_price_micros: number | null;
  chosen_price_micros: number | null;
  policy_version: string;
  reason_codes: string[];
  recorded_at: string;
  executed: boolean;
}

export interface PricingBody
  extends ViewBody<{
    latest: Panel<{ products: LatestDecision[] }>;
    history: Panel<{ products: Record<string, HistoryPoint[]>; per_product: number }>;
    recent: Panel<{ decisions: DecisionRow[] }>;
    status_counts: Panel<{ counts: { status: string; mode: string; n: number; last_as_of: string }[] }>;
  }> {
  shadow: true;
}

export interface Candidate {
  price_micros: number;
  log_ratio: number;
  expected_demand: number;
  contribution: number;
  churn_cost: number;
  delta_mean: number;
  delta_sd: number;
  delta_p05: number;
  delta_p95: number;
  delta_contribution_mean: number;
  prob_improvement: number;
  risk_adjusted: number;
  feasible: boolean;
  violations: string[];
}

export interface DecisionRecord {
  decision_id: string;
  cycle_id: string;
  product: string;
  as_of: string;
  mode: string;
  policy_version: string;
  status: string;
  current_price_micros: number | null;
  chosen_price_micros: number | null;
  reason_codes: string[];
  lineage: Record<string, string>;
  inputs: Record<string, unknown>;
  constraints: Record<string, unknown>;
  candidates: Candidate[];
  rejected_alternatives: { price_micros: number; reasons: string[] }[];
  guardrails: {
    passed?: boolean;
    checks?: Record<string, boolean>;
    [key: string]: unknown;
  };
  prediction: Prediction | null;
  errors: string[];
  is_synthetic: boolean;
}

export interface DecisionDetail {
  decision: DecisionRecord;
  record_sha256: string;
  trace_id: string | null;
  recorded_at: string;
  execution: {
    executed_at: string;
    executed_by: string;
    from_price_micros: number | null;
    to_price_micros: number;
  } | null;
  shadow: true;
  note: string;
}

// ---- payments -----------------------------------------------------------------------------
export interface CurrencySummary {
  currency: string;
  cases: number;
  open_cases: number;
  open_amount_minor: number;
  recovered: number;
  closed: number;
  recovered_minor: number;
  failed_minor: number;
  ttr_p50_hours: number | null;
  ttr_p90_hours: number | null;
  first_opened_at: string;
  last_opened_at: string;
  resolved_recovery_rate: number | null;
  rate_reason: string | null;
}

export interface OpenCase {
  invoice_id: string;
  customer_id: string;
  state: string;
  amount_minor: number;
  currency: string;
  first_reason: string | null;
  opened_at: string;
  attempts_seen: number;
  action: string | null;
  retry_at: string | null;
  p_next_success: number | null;
  policy_kind: string | null;
  policy_version: string | null;
  model_version: string | null;
  fallback_reason: string | null;
  next_retry_at: string | null;
}

export type PaymentsBody = ViewBody<{
  summary: Panel<{
    by_currency: CurrencySummary[];
    states: { state: string; n: number; amount_minor: number; currency: string }[];
    failure_reasons: { reason: string; n: number }[];
  }>;
  daily: Panel<{
    days: { day: string; opened: number; failed_minor: number; recovered: number; recovered_minor: number }[];
  }>;
  open_cases: Panel<{ cases: OpenCase[] }>;
  retries: Panel<{
    states: { state: string; n: number }[];
    upcoming: { job_id: string; invoice_id: string; attempt_number: number; run_at: string }[];
  }>;
  policies: Panel<{
    policies: {
      policy_kind: string;
      policy_version: string;
      model_version: string | null;
      decisions: number;
      fallbacks: number;
      mean_p_next_success: number | null;
      last_decided_at: string;
    }[];
  }>;
  invoices: Panel<{ states: { state: string; currency: string; n: number; amount_minor: number }[] }>;
}>;

// ---- mlops --------------------------------------------------------------------------------
export interface ModelVersion {
  model_version: string;
  model_name: string;
  stage: string;
  data_version: string;
  feature_version: string;
  code_revision: string;
  artifact_checksum: string;
  metrics: Record<string, unknown>;
  trained_at: string;
  registered_at: string;
  stage_changed_at: string;
  is_synthetic: boolean;
  validation: { passed: boolean; failed_checks: string[]; created_at: string } | null;
}

export interface MonitoringRow {
  report_id: string;
  model_name: string;
  model_version: string;
  kind: "drift" | "shadow" | "probation";
  as_of: string;
  fired: boolean;
  series: Record<string, number | null>;
}

export type MlopsBody = ViewBody<{
  versions: Panel<{ versions: ModelVersion[]; champions: Record<string, string> }>;
  events: Panel<{
    events: {
      decision_id: string;
      model_name: string;
      model_version: string;
      from_stage: string | null;
      to_stage: string;
      trigger: string;
      actor: string;
      reason: string;
      occurred_at: string;
    }[];
  }>;
  monitoring: Panel<{
    reports: MonitoringRow[];
    thresholds: {
      coverage_80_band: [number, number];
      max_vwape_ratio_vs_reference: number;
      psi_alert: number;
      source: string;
    };
  }>;
  retraining: Panel<{
    requests: {
      request_id: string;
      model_name: string;
      status: string;
      triggers: string[];
      as_of: string;
      candidate_version: string | null;
      created_at: string;
    }[];
  }>;
  serving: Panel<{
    calibration_80: Figure;
    drift_alarm: Figure;
    forecast_age_hours: Figure;
    oldest_source_age_s: Figure;
    serving_versions: string[];
    inputs: Inputs;
  }>;
}>;

// ---- executive ----------------------------------------------------------------------------
export interface ExecutiveFigure {
  figure: Figure;
  latest_day: string;
  baseline_days: number;
  definition: string;
}

export type ExecutiveKey =
  | "revenue"
  | "gross_margin"
  | "demand"
  | "active_customers"
  | "churn"
  | "recovery_rate"
  | "price_index";

export interface ExecutiveBody extends ViewBody<Record<ExecutiveKey, Panel<ExecutiveFigure>>> {
  window_days: number;
}
