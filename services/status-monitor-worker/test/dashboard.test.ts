import { describe, expect, it } from "vitest";

import type { ProbeRow, Snapshot } from "../src/db";
import { renderDashboard, seriesPayload, ttftSparkline } from "../src/dashboard";

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

  it("makes each model card a zoom target and includes the zoom overlay + client", () => {
    const html = renderDashboard(SNAPSHOT);
    expect(html).toContain('data-model="glm-4.7"');
    expect(html).toContain('role="button"');
    expect(html).toContain('id="zoom"');
    expect(html).toContain("click to zoom");
    expect(html).toContain("function openZoom");
    // The detail view charts latency, throughput, and TTFT.
    expect(html).toContain('"latencyMs"');
    expect(html).toContain('"throughputTps"');
    expect(html).toContain("tok/s");
  });

  it("embeds parseable per-model history that cannot break out of the script tag", () => {
    const withHistory: Snapshot = {
      ...SNAPSHOT,
      models: [
        {
          ...SNAPSHOT.models[0],
          modelId: "a</script>b", // hostile id must be neutralized
          history: [row(40), row(null, false), row(60)],
        },
      ],
    };
    const html = renderDashboard(withHistory);
    const json = html.split('id="model-data" type="application/json">')[1].split("</script>")[0];
    expect(json).not.toContain("</script>");
    const parsed = JSON.parse(json);
    expect(parsed["a</script>b"]).toHaveLength(3);
    expect(parsed["a</script>b"][0]).toMatchObject({ ok: 1, latencyMs: 100, ttftMs: 40 });
    expect(parsed["a</script>b"][1].ok).toBe(0);
  });

  it("caps the throughput chart so outliers above 400 tok/s are skipped", () => {
    const html = renderDashboard(SNAPSHOT);
    // Throughput is the only metric with an upper cap (5th config field = 400).
    expect(html).toContain('"throughputTps", "#4ade80", "tok/s", 400');
    // Latency and TTFT stay uncapped.
    expect(html).toContain('"latencyMs", "#60a5fa", "ms", null');
    // Points above the cap are dropped before plotting/scaling.
    expect(html).toContain("cap == null || v <= cap");
  });

  it("renders a cursor-following tooltip for chart points", () => {
    const html = renderDashboard(SNAPSHOT);
    expect(html).toContain("#chart-tip"); // styled tooltip element
    expect(html).toContain("function showTip");
    expect(html).toContain('tip.id = "chart-tip"');
    expect(html).toContain('"data-tip"'); // points expose their hover text
  });

  it("buckets probes by the fixed cron cadence to detect skipped cycles", () => {
    const html = renderDashboard(SNAPSHOT);
    // 5-minute cron interval in ms; charts floor each timestamp by this.
    expect(html).toContain("var CYCLE_MS = 300000;");
  });

  it("uses a noscript fallback for auto-refresh so the zoom view is not interrupted", () => {
    const html = renderDashboard(SNAPSHOT, undefined, 45);
    // Auto-refresh only fires without JS; with JS the client pauses it while zoomed.
    expect(html).toContain('<noscript><meta http-equiv="refresh" content="45"></noscript>');
    expect(html.match(/http-equiv="refresh"/g)).toHaveLength(1);
  });
});

describe("seriesPayload", () => {
  it("maps each model to a compact JSON-friendly history series", () => {
    const payload = seriesPayload([
      {
        modelId: "glm-4.7",
        latest: row(40),
        history: [row(40), row(null, false)],
        spark: [],
        uptimeRatio: 0.5,
      },
    ]);
    expect(Object.keys(payload)).toEqual(["glm-4.7"]);
    expect(payload["glm-4.7"]).toEqual([
      { ok: 1, t: "2026-06-13T00:00:00.000Z", latencyMs: 100, ttftMs: 40, throughputTps: 50 },
      { ok: 0, t: "2026-06-13T00:00:00.000Z", latencyMs: 100, ttftMs: null, throughputTps: 50 },
    ]);
  });

  it("preserves a model whose id is __proto__ (no prototype-setter swallow)", () => {
    const payload = seriesPayload([
      { modelId: "__proto__", latest: row(40), history: [row(40)], spark: [], uptimeRatio: 1 },
    ]);
    expect(Object.keys(payload)).toEqual(["__proto__"]);
    // Survives serialization, which is how it reaches the client.
    const json = JSON.stringify(payload);
    expect(JSON.parse(json)["__proto__"]).toHaveLength(1);
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
