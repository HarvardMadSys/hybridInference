import { describe, expect, it, vi } from "vitest";

import { createRuntimeActionExecutor } from "../src/action-executor";
import { IncidentStateMachine } from "../src/incident";
import { OutboxRunner } from "../src/outbox";
import {
  handlePrincipalQuotaRequest,
} from "../src/quota-runtime";
import { InMemoryPrincipalQuota } from "../src/quota";
import type { StagingRuntimeConfig } from "../src/runtime-config";
import {
  InMemoryIncidentStore,
  type PendingAction,
} from "../src/store";
import { envelope } from "./fakes";

function quotaNamespace(
  quota: InMemoryPrincipalQuota,
): DurableObjectNamespace {
  const stub = {
    fetch: vi.fn(async (input: RequestInfo | URL) => {
      const request =
        input instanceof Request ? input : new Request(input.toString());
      return handlePrincipalQuotaRequest(quota, request);
    }),
  };
  return {
    idFromName: vi.fn().mockReturnValue({ toString: () => "quota-id" }),
    get: vi.fn().mockReturnValue(stub),
  } as unknown as DurableObjectNamespace;
}

function slackTransport(...timestamps: readonly string[]): typeof fetch {
  const remaining = [...timestamps];
  return vi.fn(async () => {
    const ts = remaining.shift();
    if (ts === undefined) throw new Error("unexpected Slack request");
    return new Response(JSON.stringify({ ok: true, channel: "C123", ts }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

async function drain(runner: OutboxRunner, nowMs: number): Promise<void> {
  for (let index = 0; index < 50; index += 1) {
    const result = await runner.runOne(nowMs);
    if (!result.ran) return;
  }
  throw new Error("runtime outbox did not settle");
}

function action(
  store: InMemoryIncidentStore,
  type: PendingAction["type"],
): PendingAction {
  const result = store.listActions().find((candidate) => candidate.type === type);
  if (!result) throw new Error(`missing ${type}`);
  return result;
}

describe("configured Phase C1 runtime", () => {
  it("completes the Slack lifecycle with durable quota while analysis fails explicitly", async () => {
    const store = new InMemoryIncidentStore();
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 5,
      pendingLeaseMs: 120_000,
      now: () => 1_000,
    });
    const config: StagingRuntimeConfig = {
      mode: "staging-runtime",
      routeKey: "runtime-lifecycle-route-key-material-32-bytes-minimum",
      slack: {
        botToken: "xoxb-unit-test-token-123456",
        channelId: "C123",
        sinkId: "slack-staging",
      },
      quota: {
        activeLimit: 5,
        pendingLeaseMs: 120_000,
        namespace: quotaNamespace(quota),
      },
    };
    const fetch = slackTransport(
      "1784505600.000001",
      "1784505600.000001",
      "1784505602.000001",
    );
    const machine = new IncidentStateMachine(store, {
      idFactory: (() => {
        let sequence = 0;
        return (kind) => `${kind}-${sequence++}`;
      })(),
      repeatUpdateDelayMs: 5,
      actionDeadlineMs: 1_000_000,
      analysisDeadlineMs: 1_000_000,
    });
    const executor = createRuntimeActionExecutor(store, config, {
      fetch,
      now: () => 1_000,
    });
    const runner = new OutboxRunner(store, executor, {
      hooks: {
        onActionCompleted: machine.onActionCompleted.bind(machine),
        onActionTerminal: machine.onActionTerminal.bind(machine),
      },
    });

    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      1_000,
    );
    await drain(runner, 1_000);
    expect(action(store, "reserve_quota")).toMatchObject({
      status: "completed",
      result: { admitted: true, lease_epoch: 1 },
    });
    expect(action(store, "post_parent").status).toBe("completed");
    expect(action(store, "dispatch_analysis")).toMatchObject({
      status: "failed",
      lastError: "analysis_not_enabled",
    });
    expect(store.listAnalysisJobs()[0]).toMatchObject({
      status: "failed",
      lastError: "analysis_not_enabled",
    });

    machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:01.000Z"),
      "sha256:f2",
      1_005,
    );
    await drain(runner, 1_010);
    expect(action(store, "update_parent").status).toBe("completed");

    machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:02.000Z"),
      "sha256:r1",
      1_020,
    );
    await drain(runner, 1_020);
    expect(action(store, "post_recovery").status).toBe("completed");
    expect(action(store, "release_quota").status).toBe("completed");
    expect(store.getGeneration(1)).toMatchObject({
      state: "resolved",
      quotaState: "released",
      deliveryRef: {
        sinkId: "slack-staging",
        destinationId: "C123",
        messageId: "1784505600.000001",
      },
    });
    expect(quota.activeCount("staging", "staging-gateway")).toBe(0);
    expect(fetch).toHaveBeenCalledTimes(3);
  });
});
