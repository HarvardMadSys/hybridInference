import { describe, expect, it } from "vitest";

import { IncidentStateMachine } from "../src/incident";
import { NotificationActionExecutor } from "../src/notification-executor";
import {
  type ActionClaim,
  type ActionExecutionResult,
  type ActionExecutor,
  OutboxRunner,
} from "../src/outbox";
import {
  type IncidentGeneration,
  InMemoryIncidentStore,
  type PendingAction,
} from "../src/store";
import { SlackSink } from "../src/slack";
import {
  deterministicIds,
  envelope,
  pendingAction,
  ThreadedFakeSink,
  ThreadlessFakeSink,
} from "./fakes";

const NOTIFICATION_TYPES: ReadonlySet<PendingAction["type"]> = new Set([
  "post_parent",
  "update_parent",
  "post_recovery",
  "post_analysis",
]);

/** Handles the non-notification (infrastructure) actions the state machine emits. */
class InfraExecutor implements ActionExecutor {
  async execute(claim: ActionClaim): Promise<ActionExecutionResult> {
    switch (claim.action.type) {
      case "reserve_quota":
        return {
          outcome: "success",
          result: { admitted: true, lease_epoch: claim.action.generation },
        };
      case "release_quota":
        return { outcome: "success", result: {} };
      case "dispatch_analysis":
        return { outcome: "success", result: { githubRunId: `run-${claim.action.generation}` } };
      default:
        throw new Error(`InfraExecutor received unexpected action: ${claim.action.type}`);
    }
  }
}

/** Routes the four notification types to the sink executor; everything else to infra. */
class CompositeExecutor implements ActionExecutor {
  constructor(
    private readonly notification: NotificationActionExecutor,
    private readonly infra: ActionExecutor,
  ) {}

  async execute(claim: ActionClaim): Promise<ActionExecutionResult> {
    if (NOTIFICATION_TYPES.has(claim.action.type)) return this.notification.execute(claim);
    return this.infra.execute(claim);
  }
}

function actionOf(
  store: InMemoryIncidentStore,
  type: PendingAction["type"],
  generation = 1,
): PendingAction {
  const action = store
    .listActions()
    .find((candidate) => candidate.type === type && candidate.generation === generation);
  if (!action) throw new Error(`missing ${type} action for generation ${generation}`);
  return action;
}

async function drain(runner: OutboxRunner, nowMs: number, max = 100): Promise<void> {
  for (let i = 0; i < max; i += 1) {
    const result = await runner.runOne(nowMs);
    if (!result.ran) return;
  }
  throw new Error("outbox did not settle within the iteration budget");
}

interface SlackFetchCall {
  readonly url: string;
  readonly init: RequestInit | undefined;
}

function slackTransport(...timestamps: readonly string[]): {
  readonly fetch: typeof fetch;
  readonly calls: SlackFetchCall[];
} {
  const remaining = [...timestamps];
  const calls: SlackFetchCall[] = [];
  const fake = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    calls.push({ url, init });
    const ts = remaining.shift();
    if (ts === undefined) throw new Error("unexpected Slack request");
    return new Response(JSON.stringify({ ok: true, channel: "C123", ts }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  return { fetch: fake as typeof fetch, calls };
}

/**
 * Inject a synthetic `post_analysis` action. The Phase A state machine does not
 * yet wire the analysis callback into a `post_analysis` transition, so this
 * exercises the notification contract for that action through the real outbox.
 */
function injectPostAnalysis(
  store: InMemoryIncidentStore,
  generation: IncidentGeneration,
  nowMs: number,
): void {
  store.transaction(() => {
    store.putAction(
      pendingAction({
        actionId: `post-analysis-${generation.generation}`,
        incidentId: generation.incidentId,
        generation: generation.generation,
        type: "post_analysis",
        status: "pending",
        stateVersion: generation.stateVersion,
        resolutionEpoch: generation.resolutionEpoch,
        payload: {
          incident_id: generation.incidentId,
          generation: generation.generation,
          occurrence_count: generation.occurrenceCount,
          envelope: generation.latestEnvelope,
          analysis: { summary: "root cause confirmed" },
          state_version: generation.stateVersion,
        },
        nextRunAtMs: nowMs,
        finalDeadlineAtMs: nowMs + 1_000_000,
        createdAtMs: nowMs,
        updatedAtMs: nowMs,
      }),
    );
  });
}

const sinkCases = [
  { label: "threaded", make: () => new ThreadedFakeSink() },
  { label: "threadless", make: () => new ThreadlessFakeSink() },
] as const;

describe.each(sinkCases)("incident lifecycle with the $label sink", ({ label, make }) => {
  function harness(sink: ThreadedFakeSink | ThreadlessFakeSink) {
    const store = new InMemoryIncidentStore();
    const machine = new IncidentStateMachine(store, {
      idFactory: deterministicIds(),
      repeatUpdateDelayMs: 5,
      actionDeadlineMs: 1_000_000,
      analysisDeadlineMs: 1_000_000,
    });
    const executor = new CompositeExecutor(
      new NotificationActionExecutor(store, sink),
      new InfraExecutor(),
    );
    const runner = new OutboxRunner(store, executor, {
      claimLeaseMs: 1_000,
      hooks: {
        onActionCompleted: machine.onActionCompleted.bind(machine),
        onActionTerminal: machine.onActionTerminal.bind(machine),
      },
    });
    return { store, machine, runner };
  }

  it("completes firing -> repeat -> resolved -> re-fire (+ post_analysis)", async () => {
    const sink = make();
    const { store, machine, runner } = harness(sink);

    // 1. Firing opens generation 1 and posts exactly one parent.
    machine.applyEvent(envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"), "sha256:f1", 0);
    await drain(runner, 0);
    expect(actionOf(store, "post_parent", 1).status).toBe("completed");
    const gen1 = store.getGeneration(1);
    expect(gen1?.state).toBe("firing");
    expect(gen1?.deliveryRef).not.toBeNull();
    expect(gen1?.deliveryRef?.sinkId).toBe("slack-primary");
    if (label === "threaded") {
      expect(gen1?.deliveryRef?.conversationId).toBeDefined();
    } else {
      expect(gen1?.deliveryRef?.conversationId).toBeUndefined();
    }
    // dispatch_analysis ran through the infra executor.
    expect(actionOf(store, "dispatch_analysis", 1).status).toBe("completed");

    // 1b. A fenced analysis reply threads onto the same delivery reference.
    injectPostAnalysis(store, store.getGeneration(1)!, 1);
    await drain(runner, 1);
    expect(actionOf(store, "post_analysis", 1).status).toBe("completed");

    // 2. Repeat coalesces into a single parent update.
    machine.applyEvent(envelope("firing-2", "firing", "2026-07-20T00:00:01.000Z"), "sha256:f2", 5);
    await drain(runner, 10);
    expect(actionOf(store, "update_parent", 1).status).toBe("completed");
    expect(store.getGeneration(1)?.occurrenceCount).toBe(2);

    // 3. Resolution posts recovery on the same reference and resolves.
    machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:02.000Z"),
      "sha256:r1",
      20,
    );
    await drain(runner, 20);
    expect(actionOf(store, "post_recovery", 1).status).toBe("completed");
    expect(store.getGeneration(1)?.state).toBe("resolved");

    // 4. Re-fire opens a brand-new generation with its own single parent.
    machine.applyEvent(envelope("firing-3", "firing", "2026-07-20T00:00:03.000Z"), "sha256:f3", 30);
    await drain(runner, 30);
    expect(actionOf(store, "post_parent", 2).status).toBe("completed");
    const gen2 = store.getGeneration(2);
    expect(gen2?.state).toBe("firing");
    expect(gen2?.deliveryRef).not.toBeNull();

    // Exactly one parent per generation and no cross-generation reference reuse.
    const parents = store.listActions().filter((action) => action.type === "post_parent");
    expect(parents).toHaveLength(2);
    expect(parents.every((action) => action.status === "completed")).toBe(true);
    expect(gen1?.deliveryRef?.messageId).not.toBe(gen2?.deliveryRef?.messageId);

    // The sink observed the full platform-agnostic lifecycle.
    const kinds = sink.calls.map((call) => call.action.type);
    expect(kinds.filter((kind) => kind === "post_parent")).toHaveLength(2);
    expect(kinds).toContain("update_parent");
    expect(kinds).toContain("post_recovery");
    expect(kinds).toContain("post_analysis");
  });
});

describe("incident lifecycle with the real Slack sink boundary", () => {
  it("renders and sends parent, update, and recovery actions from real incident payloads", async () => {
    const store = new InMemoryIncidentStore();
    const machine = new IncidentStateMachine(store, {
      idFactory: deterministicIds(),
      repeatUpdateDelayMs: 5,
      actionDeadlineMs: 1_000_000,
      analysisDeadlineMs: 1_000_000,
    });
    const parentTs = "1784505600.000001";
    const recoveryTs = "1784505602.000001";
    const transport = slackTransport(parentTs, parentTs, recoveryTs);
    const sink = new SlackSink({
      sinkId: "slack-primary",
      botToken: "unit-test-token",
      channelId: "C123",
      fetch: transport.fetch,
      now: () => Date.parse("2026-07-20T00:00:10.000Z"),
    });
    const executor = new CompositeExecutor(
      new NotificationActionExecutor(store, sink),
      new InfraExecutor(),
    );
    const runner = new OutboxRunner(store, executor, {
      claimLeaseMs: 30_000,
      hooks: {
        onActionCompleted: machine.onActionCompleted.bind(machine),
        onActionTerminal: machine.onActionTerminal.bind(machine),
      },
    });

    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    await drain(runner, 0);

    machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:01.000Z"),
      "sha256:f2",
      5,
    );
    await drain(runner, 10);

    machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:02.000Z"),
      "sha256:r1",
      20,
    );
    await drain(runner, 20);

    const parent = actionOf(store, "post_parent");
    const update = actionOf(store, "update_parent");
    const recovery = actionOf(store, "post_recovery");
    for (const action of [parent, update, recovery]) {
      expect(action).toMatchObject({ status: "completed", lastError: null });
      expect(action.result).toBeDefined();
    }
    expect(store.getGeneration(1)).toMatchObject({
      state: "resolved",
      deliveryRef: {
        sinkId: "slack-primary",
        platform: "slack",
        destinationId: "C123",
        messageId: parentTs,
        conversationId: parentTs,
      },
    });

    expect(transport.calls.map((call) => call.url)).toEqual([
      "https://slack.com/api/chat.postMessage",
      "https://slack.com/api/chat.update",
      "https://slack.com/api/chat.postMessage",
    ]);
    const bodies = transport.calls.map(
      (call) => JSON.parse(String(call.init?.body)) as Record<string, unknown>,
    );
    expect(bodies[0]).toMatchObject({ channel: "C123" });
    expect(bodies[0]).not.toHaveProperty("thread_ts");
    expect(bodies[1]).toMatchObject({ channel: "C123", ts: parentTs });
    expect(bodies[1]).not.toHaveProperty("thread_ts");
    expect(bodies[2]).toMatchObject({ channel: "C123", thread_ts: parentTs });

    for (const [body, action] of [
      [bodies[0], parent],
      [bodies[1], update],
      [bodies[2], recovery],
    ] as const) {
      expect(body.metadata).toEqual({
        event_type: "alert_control_plane_action",
        event_payload: {
          action_id: action.actionId,
          incident_id: action.incidentId,
          generation: action.generation,
          payload_digest: expect.stringMatching(/^sha256:[0-9a-f]{64}$/),
        },
      });
    }
  });
});
