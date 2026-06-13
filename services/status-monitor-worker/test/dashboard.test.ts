import { describe, expect, it } from "vitest";

import type { Snapshot } from "../src/db";
import { renderDashboard } from "../src/dashboard";

const SNAPSHOT: Snapshot = {
  total: 2,
  healthy: 1,
  unhealthy: 1,
  cycle: { ok: true, checkedAt: "2026-06-13T00:00:00.000Z", error: null },
  models: [
    {
      modelId: "glm-4.7",
      latest: {
        ok: true,
        checkedAt: "2026-06-13T00:00:00.000Z",
        latencyMs: 120,
        ttftMs: 40,
        completionTokens: 8,
        throughputTps: 66.7,
        error: null,
      },
      history: [],
      spark: [],
      uptimeRatio: 1,
    },
    {
      modelId: "bge-m3",
      latest: {
        ok: false,
        checkedAt: "2026-06-13T00:00:00.000Z",
        latencyMs: 10,
        ttftMs: null,
        completionTokens: null,
        throughputTps: null,
        error: "HTTP 503",
      },
      history: [],
      spark: [],
      uptimeRatio: 0,
    },
  ],
};

describe("renderDashboard", () => {
  it("renders summary counts and per-model cards", () => {
    const html = renderDashboard(SNAPSHOT);
    expect(html).toContain("FreeInference Model Status");
    expect(html).toContain("1 up");
    expect(html).toContain("1 down");
    expect(html).toContain("glm-4.7");
    expect(html).toContain("HTTP 503");
  });

  it("shows a placeholder when there are no models", () => {
    const html = renderDashboard({
      total: 0,
      healthy: 0,
      unhealthy: 0,
      cycle: { ok: true, checkedAt: null, error: null },
      models: [],
    });
    expect(html).toContain("first cron cycle is pending");
  });

  it("renders a banner when the last cycle failed", () => {
    const html = renderDashboard({
      ...SNAPSHOT,
      cycle: { ok: false, checkedAt: "2026-06-13T00:05:00.000Z", error: "HTTP 502" },
    });
    expect(html).toContain("Last probe cycle failed");
    expect(html).toContain("HTTP 502");
  });
});
