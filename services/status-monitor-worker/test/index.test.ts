import { describe, expect, it } from "vitest";

import { isAccountLevelFailure } from "../src/index";
import type { ProbeResult } from "../src/probe";

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
