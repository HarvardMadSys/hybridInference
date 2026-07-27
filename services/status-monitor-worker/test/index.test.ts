import { describe, expect, it, vi } from "vitest";

import worker, { isAccountLevelFailure } from "../src/index";
import type { ProbeResult } from "../src/probe";

vi.mock("../src/db", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getSnapshot: vi.fn(async () => ({
    models: [],
    total: 2,
    healthy: 2,
    unhealthy: 0,
    cycle: { ok: true, checkedAt: "2026-07-25T00:00:00Z", error: null },
  })),
}));
vi.mock("../src/control-plane", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  countPendingCanonicalEvents: vi.fn(async () => 2),
}));

function result(ok: boolean, error: string | null): ProbeResult {
  return {
    modelId: "m",
    ok,
    checkedAt: "2026-06-13T00:00:00Z",
    latencyMs: 1,
    ttftMs: null,
    completionTokens: null,
    throughputTps: null,
    error,
  };
}

describe("isAccountLevelFailure", () => {
  it("is true when every probe failed with a gateway account-level message", () => {
    expect(
      isAccountLevelFailure([
        result(false, "HTTP 401: Invalid or expired API key"),
        result(false, "HTTP 403: Email not verified. Please verify your email to continue."),
        result(false, "HTTP 429: Daily cost quota exceeded"),
      ]),
    ).toBe(true);
  });

  it("is false when any probe succeeded", () => {
    expect(
      isAccountLevelFailure([result(true, null), result(false, "HTTP 401: Invalid or expired API key")]),
    ).toBe(false);
  });

  it("is false for upstream failures even with account-ish status codes", () => {
    // A provider 401/429 surfaces with a different message (or as 500), so it
    // must be recorded as a real outage, not suppressed.
    expect(
      isAccountLevelFailure([
        result(false, "HTTP 401: upstream provider rejected key"),
        result(false, "HTTP 500: Internal server error (req_x)"),
      ]),
    ).toBe(false);
  });

  it("is false for an empty result set", () => {
    expect(isAccountLevelFailure([])).toBe(false);
  });
});

describe("GET /api/health", () => {
  it("exposes stalled control-plane transitions without flipping health", async () => {
    const response = await worker.fetch(
      new Request("https://monitor.test/api/health"),
      {} as never,
    );

    // Rejection reports only reach `wrangler tail`; this field is the way a
    // stalled pipeline (a count that persists across ~20min cycles) becomes
    // visible to an uptime check. It reports, but never flips `ok`, because a
    // cycle in flight can transiently hold a pending row.
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      status: "ok",
      pendingControlPlaneTransitions: 2,
    });
  });
});
