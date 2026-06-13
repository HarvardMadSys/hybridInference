import { describe, expect, it } from "vitest";

import type { ProbeRow, Snapshot } from "../src/db";
import { renderDashboard, ttftSparkline } from "../src/dashboard";

function row(ttftMs: number | null, ok = true): ProbeRow {
  return {
    ok,
    checkedAt: "2026-06-13T00:00:00.000Z",
    latencyMs: 100,
    ttftMs,
    completionTokens: 5,
    throughputTps: 50,
    error: ok ? null : "HTTP 503",
  };
}

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

describe("ttftSparkline", () => {
  it("renders a polyline with one point per probe that has a ttft", () => {
    const html = ttftSparkline([row(40), row(60), row(50)]);
    expect(html).toContain("TTFT trend");
    expect(html).toContain("<polyline");
    expect(html).toContain("50 ms"); // latest value shown in the header
    const points = (html.match(/points="([^"]+)"/)?.[1] ?? "").trim().split(/\s+/);
    expect(points).toHaveLength(3);
  });

  it("skips failed probes (null ttft) but keeps their time position", () => {
    const html = ttftSparkline([row(40), row(null, false), row(80)]);
    const points = (html.match(/points="([^"]+)"/)?.[1] ?? "").trim().split(/\s+/);
    expect(points).toHaveLength(2); // only the two non-null points are plotted
    expect(points[0].startsWith("0.0,")).toBe(true); // first probe at the left edge
    expect(points[1].startsWith("100.0,")).toBe(true); // third probe at the right edge
  });

  it("shows a fallback when there are fewer than two data points", () => {
    expect(ttftSparkline([row(40)])).toContain("not enough data");
    expect(ttftSparkline([])).toContain("not enough data");
  });
});
