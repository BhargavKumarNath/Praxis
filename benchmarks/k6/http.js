// Phase 14 W3 (ADR 0022): HTTP read surfaces under a fixed arrival rate. Local only:
// the core API and the decision console against the scale run's Postgres. Never Stripe,
// never a public API. Pass/fail is decided by configs/scale/slo.toml, not by k6 thresholds.
import http from "k6/http";
import { check } from "k6";

const API = __ENV.API_URL || "http://127.0.0.1:8000";
const CONSOLE = __ENV.CONSOLE_URL || "http://127.0.0.1:8100";
const RATE = Number(__ENV.RATE || 50); // requests per second, per scenario
const DURATION = __ENV.DURATION || "60s";

function scenario(exec) {
  return {
    executor: "constant-arrival-rate",
    rate: RATE,
    timeUnit: "1s",
    duration: DURATION,
    preAllocatedVUs: 20,
    maxVUs: 200,
    exec,
  };
}

export const options = {
  scenarios: {
    api: scenario("api"),
    console: scenario("consoleReads"),
  },
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
  // Non-gating thresholds: they only make k6 export per-surface sub-metrics.
  thresholds: {
    "http_req_duration{surface:api}": ["max>=0"],
    "http_req_duration{surface:console}": ["max>=0"],
    "http_req_failed{surface:api}": ["rate>=0"],
    "http_req_failed{surface:console}": ["rate>=0"],
  },
};

const API_PATHS = ["/healthz", "/v1/ops/health"];
const CONSOLE_PATHS = ["/v1/console/system", "/v1/console/payments", "/v1/console/operator"];

function get(base, paths, surface) {
  const path = paths[Math.floor(Math.random() * paths.length)];
  const res = http.get(base + path, { tags: { surface, path } });
  check(res, { "status 200": (r) => r.status === 200 });
}

export function api() {
  get(API, API_PATHS, "api");
}

export function consoleReads() {
  get(CONSOLE, CONSOLE_PATHS, "console");
}
