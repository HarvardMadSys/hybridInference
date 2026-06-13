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
  it("is true when every probe failed with 401/403/429", () => {
    expect(
      isAccountLevelFailure([
        result(false, "HTTP 401"),
        result(false, "HTTP 403"),
        result(false, "Error: HTTP 429"),
      ]),
    ).toBe(true);
  });

  it("is false when any probe succeeded", () => {
    expect(isAccountLevelFailure([result(true, null), result(false, "HTTP 403")])).toBe(false);
  });

  it("is false for ordinary provider outages (e.g. 503)", () => {
    expect(isAccountLevelFailure([result(false, "HTTP 503"), result(false, "timeout")])).toBe(
      false,
    );
  });

  it("is false for an empty result set", () => {
    expect(isAccountLevelFailure([])).toBe(false);
  });
});
