// The only way the console talks to the server. Requests go to /api/console/*, which Next
// rewrites to the console API (next.config.ts). A failed call becomes an ApiError that keeps
// the correlation id and a fixed message; server internals never reach the screen.

import type { ApiError } from "./types";

export const API_BASE = "/api/console";
const CORRELATION_HEADER = "X-Correlation-ID";

export type Result<T> =
  | { ok: true; data: T; correlationId: string | null }
  | { ok: false; error: ApiError };

function safeError(status: number, body: unknown, correlationId: string | null): ApiError {
  const b = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  const error = typeof b.error === "string" ? b.error : status === 0 ? "network_error" : "http_error";
  // Only the server's own short, fixed detail strings are shown; anything else is dropped.
  const detail = typeof b.detail === "string" && b.detail.length <= 300 ? b.detail : null;
  const cid =
    typeof b.correlation_id === "string" ? b.correlation_id : correlationId;
  return { status, error, correlationId: cid, detail };
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  fetchImpl: typeof fetch = fetch,
): Promise<Result<T>> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    return { ok: false, error: safeError(0, null, null) };
  }
  const correlationId = response.headers.get(CORRELATION_HEADER);
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    return { ok: false, error: safeError(response.status, body, correlationId) };
  }
  return { ok: true, data: body as T, correlationId };
}

export function authHeader(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

export function describeError(error: ApiError): string {
  switch (error.error) {
    case "network_error":
      return "The console API did not respond. Check that it is running (make console-api).";
    case "internal_error":
      return "The console API failed while reading this view.";
    case "store_unavailable":
      return "A store behind this view did not answer.";
    case "not_configured":
      return "The control-plane database is not configured for the console API.";
    case "not_found":
      return error.detail ?? "Nothing with this id exists.";
    case "unauthorized":
      return "The approver token was not accepted.";
    case "admin_not_configured":
      return "No approvers are configured: the console is read-only.";
    case "refused":
      return error.detail ?? "The approval path refused this action.";
    case "invalid_request":
      return error.detail ?? "The request was not valid.";
    default:
      return `The console API answered ${error.status}.`;
  }
}
