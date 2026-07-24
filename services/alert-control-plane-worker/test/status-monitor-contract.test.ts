import { describe, expect, it } from "vitest";

import { modelUnavailableEvent } from "../../status-monitor-worker/src/control-plane";
import type { ProbeResult } from "../../status-monitor-worker/src/probe";
import { parseAlertEvent } from "../src/validation";

const TEST_NOW = Date.parse("2026-07-24T02:00:00Z");

function result(overrides: Partial<ProbeResult> = {}): ProbeResult {
  return {
    modelId: "deepseek-v3",
    ok: false,
    checkedAt: "2026-07-24T01:00:00Z",
    latencyMs: null,
    ttftMs: null,
    completionTokens: null,
    throughputTps: null,
    error: "request timed out",
    ...overrides,
  };
}

describe("status-monitor to control-plane contract", () => {
  it("accepts the exact firing and resolved bodies emitted by the source adapter", () => {
    const firingBody = modelUnavailableEvent(result(), "firing", 2, "contract-firing-1");
    const resolvedBody = modelUnavailableEvent(
      result({
        ok: true,
        checkedAt: "2026-07-24T01:20:00Z",
        latencyMs: 842,
        error: null,
      }),
      "resolved",
      2,
      "contract-resolved-1",
    );

    const firing = parseAlertEvent(JSON.parse(JSON.stringify(firingBody)), { now: TEST_NOW });
    const resolved = parseAlertEvent(JSON.parse(JSON.stringify(resolvedBody)), { now: TEST_NOW });

    expect(firing).toMatchObject({
      event_id: "contract-firing-1",
      alert_type: "model_unavailable",
      status: "firing",
      occurred_at: "2026-07-24T01:00:00.000Z",
      context: {
        model_id: "deepseek-v3",
        consecutive_failures: 2,
        failure_threshold: 2,
        reason: "timeout",
      },
    });
    expect(resolved).toMatchObject({
      event_id: "contract-resolved-1",
      alert_type: "model_unavailable",
      status: "resolved",
      occurred_at: "2026-07-24T01:20:00.000Z",
      context: {
        model_id: "deepseek-v3",
        latency_ms: 842,
      },
    });
  });
});
