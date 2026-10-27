import { describe, expect, it } from "vitest";

import { request } from "../lib/api";

const respond = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  (async () =>
    new Response(body === undefined ? "not json" : JSON.stringify(body), {
      status,
      headers,
    })) as typeof fetch;

describe("request", () => {
  it("keeps the correlation id of a failed call and only the fixed error code", async () => {
    const r = await request(
      "/system",
      {},
      respond(500, { error: "internal_error", correlation_id: "cid-body" }, { "X-Correlation-ID": "cid-header" }),
    );
    expect(r).toEqual({
      ok: false,
      error: { status: 500, error: "internal_error", correlationId: "cid-body", detail: null },
    });
  });

  it("falls back to the response header when the body carries no correlation id", async () => {
    const r = await request("/system", {}, respond(502, undefined, { "X-Correlation-ID": "cid-header" }));
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.error.correlationId).toBe("cid-header");
      expect(r.error.error).toBe("http_error");
    }
  });

  it("drops an oversized detail instead of showing it", async () => {
    const r = await request("/x", {}, respond(409, { error: "refused", detail: "x".repeat(301) }));
    expect(!r.ok && r.error.detail).toBe(null);
  });

  it("reports an unreachable API as a network error", async () => {
    const r = await request("/x", {}, (async () => {
      throw new TypeError("connection refused");
    }) as typeof fetch);
    expect(r).toEqual({
      ok: false,
      error: { status: 0, error: "network_error", correlationId: null, detail: null },
    });
  });
});
