import { describe, expect, it } from "vitest";

import {
  EventIdConflictError,
  IncidentStateMachine,
} from "../src/incident";
import {
  DurableObjectSqlStore,
  InMemoryIncidentStore,
  SQL_SCHEMA,
  type PendingAction,
} from "../src/store";
import { deliveryRef, deterministicIds, envelope } from "./fakes";

function harness() {
  const store = new InMemoryIncidentStore();
  const machine = new IncidentStateMachine(store, {
    idFactory: deterministicIds(),
    repeatUpdateDelayMs: 10,
    actionDeadlineMs: 1_000,
    analysisDeadlineMs: 500,
  });
  return { store, machine };
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

function completeAction(
  store: InMemoryIncidentStore,
  machine: IncidentStateMachine,
  type: PendingAction["type"],
  result: Record<string, unknown>,
  nowMs: number,
  generation = 1,
): void {
  store.transaction(() => {
    const action = actionOf(store, type, generation);
    action.status = "completed";
    action.result = result;
    store.putAction(action);
    machine.onActionCompleted(action, result, nowMs);
  });
}

function admitQuota(
  store: InMemoryIncidentStore,
  machine: IncidentStateMachine,
  nowMs: number,
  generation = 1,
): void {
  completeAction(
    store,
    machine,
    "reserve_quota",
    { admitted: true, lease_epoch: generation },
    nowMs,
    generation,
  );
}

describe("IncidentStateMachine", () => {
  it("returns the original acknowledgement for an exact duplicate and rejects digest conflicts", () => {
    const { store, machine } = harness();
    const event = envelope("event-1", "firing", "2026-07-20T00:00:00.000Z");

    const first = machine.applyEvent(event, "sha256:first", 0);
    const duplicate = machine.applyEvent(event, "sha256:first", 1);

    expect(duplicate).toEqual(first);
    expect(store.snapshot().generations).toHaveLength(1);
    expect(store.snapshot().receipts).toHaveLength(1);
    expect(store.snapshot().actions.filter((action) => action.type === "post_parent")).toHaveLength(
      1,
    );
    expect(() => machine.applyEvent(event, "sha256:conflict", 2)).toThrow(
      EventIdConflictError,
    );
  });

  it("records orphan resolution high-watermark and never opens for older or equal-time firing", () => {
    const { store, machine } = harness();
    const resolved = machine.applyEvent(
      envelope("resolved-z", "resolved", "2026-07-20T00:00:02.000Z"),
      "sha256:resolved",
      0,
    );
    const older = machine.applyEvent(
      envelope("firing-old", "firing", "2026-07-20T00:00:01.000Z"),
      "sha256:older",
      1,
    );
    const equalTime = machine.applyEvent(
      envelope("firing-equal", "firing", "2026-07-20T00:00:02.000Z"),
      "sha256:equal",
      2,
    );

    expect(resolved.action).toBe("orphan_resolution");
    expect(older.action).toBe("stale");
    expect(equalTime.action).toBe("stale");
    expect(store.snapshot().generations).toEqual([]);

    const newer = machine.applyEvent(
      envelope("firing-new", "firing", "2026-07-20T00:00:03.000Z"),
      "sha256:newer",
      3,
    );
    expect(newer.action).toBe("opened");
    expect(newer.generation).toBe(1);
  });

  it("uses event ID byte order as the stable same-status tie breaker", () => {
    const { store, machine } = harness();
    const occurredAt = "2026-07-20T00:00:00.000Z";
    machine.applyEvent(envelope("z", "firing", occurredAt), "sha256:z", 0);
    const stale = machine.applyEvent(envelope("a", "firing", occurredAt), "sha256:a", 1);
    const repeat = machine.applyEvent(envelope("zz", "firing", occurredAt), "sha256:zz", 2);

    expect(stale.action).toBe("stale");
    expect(repeat.action).toBe("repeated");
    expect(store.getLatestGeneration()?.occurrenceCount).toBe(2);
  });

  it("coalesces repeats into the unclaimed parent action while opening", () => {
    const { store, machine } = harness();
    machine.applyEvent(
      envelope("event-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:1",
      0,
    );
    machine.applyEvent(
      envelope("event-2", "firing", "2026-07-20T00:00:01.000Z"),
      "sha256:2",
      1,
    );

    const parents = store.listActions().filter((action) => action.type === "post_parent");
    expect(parents).toHaveLength(1);
    expect(parents[0].status).toBe("blocked");
    expect(parents[0].version).toBe(2);
    expect(parents[0].payload.occurrence_count).toBe(2);
    expect(store.getLatestGeneration()).toMatchObject({
      state: "opening",
      occurrenceCount: 2,
    });
  });

  it("never makes the parent runnable before principal quota is confirmed", () => {
    const { store, machine } = harness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );

    expect(actionOf(store, "reserve_quota").status).toBe("pending");
    expect(actionOf(store, "post_parent")).toMatchObject({
      status: "blocked",
      dependsOnActionId: actionOf(store, "reserve_quota").actionId,
    });

    admitQuota(store, machine, 1);

    expect(store.getGeneration(1)).toMatchObject({
      quotaState: "confirmed",
      quotaLeaseEpoch: 1,
    });
    expect(actionOf(store, "post_parent")).toMatchObject({
      status: "pending",
      dependsOnActionId: null,
    });
  });

  it("persists quota suppression without creating a Slack or analysis side effect", () => {
    const { store, machine } = harness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    completeAction(store, machine, "reserve_quota", { admitted: false }, 1);

    expect(store.getGeneration(1)).toMatchObject({
      state: "suppressed",
      quotaState: "suppressed",
      deliveryRef: null,
    });
    expect(actionOf(store, "post_parent").status).toBe("cancelled");
    expect(store.listAnalysisJobs()[0]).toMatchObject({
      status: "failed",
      lastError: "principal active quota exceeded",
    });

    const repeat = machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:02.000Z"),
      "sha256:f2",
      2,
    );
    const resolved = machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:03.000Z"),
      "sha256:r1",
      3,
    );
    expect(repeat.action).toBe("quota_suppressed");
    expect(resolved.action).toBe("resolved_suppressed");
    expect(store.getGeneration(1)?.state).toBe("resolved");
    expect(
      store
        .listActions()
        .filter((action) => action.type === "post_parent" || action.type === "post_recovery")
        .every((action) => action.status === "cancelled"),
    ).toBe(true);
  });

  it("cancels an unclaimed recovery and reopens the same generation with a new epoch", () => {
    const { store, machine } = harness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:01.000Z"),
      "sha256:r1",
      1,
    );
    const reopened = machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:02.000Z"),
      "sha256:f2",
      2,
    );

    expect(reopened).toMatchObject({ action: "reopened", generation: 1 });
    expect(store.getLatestGeneration()).toMatchObject({
      state: "opening",
      generation: 1,
      resolutionEpoch: 2,
      occurrenceCount: 2,
    });
    expect(actionOf(store, "post_recovery").status).toBe("cancelled");
  });

  it("cancels an unclaimed parent update before recovery and fences recovery behind an in-flight update", () => {
    const unclaimed = harness();
    unclaimed.machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    admitQuota(unclaimed.store, unclaimed.machine, 1);
    completeAction(
      unclaimed.store,
      unclaimed.machine,
      "post_parent",
      { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } },
      1,
    );
    unclaimed.machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:02.000Z"),
      "sha256:f2",
      2,
    );
    unclaimed.machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:03.000Z"),
      "sha256:r1",
      3,
    );
    expect(actionOf(unclaimed.store, "update_parent").status).toBe("cancelled");
    expect(actionOf(unclaimed.store, "post_recovery")).toMatchObject({
      status: "pending",
      dependsOnActionId: null,
    });

    const inFlight = harness();
    inFlight.machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    admitQuota(inFlight.store, inFlight.machine, 1);
    completeAction(
      inFlight.store,
      inFlight.machine,
      "post_parent",
      { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } },
      1,
    );
    inFlight.machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:02.000Z"),
      "sha256:f2",
      2,
    );
    inFlight.store.transaction(() => {
      const update = actionOf(inFlight.store, "update_parent");
      update.status = "claimed";
      update.claimEpoch = 1;
      update.leaseExpiresAtMs = 100;
      inFlight.store.putAction(update);
    });
    inFlight.machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:03.000Z"),
      "sha256:r1",
      3,
    );
    const update = actionOf(inFlight.store, "update_parent");
    expect(actionOf(inFlight.store, "post_recovery")).toMatchObject({
      status: "blocked",
      dependsOnActionId: update.actionId,
    });
  });

  it.each(["claimed", "uncertain"] as const)(
    "freezes a %s recovery and materializes the queued firing only after recovery completes",
    (recoveryStatus) => {
      const { store, machine } = harness();
      machine.applyEvent(
        envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
        "sha256:f1",
        0,
      );
      admitQuota(store, machine, 1);
      completeAction(store, machine, "post_parent", { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } }, 1);
      machine.applyEvent(
        envelope("resolved-1", "resolved", "2026-07-20T00:00:02.000Z"),
        "sha256:r1",
        2,
      );
      store.transaction(() => {
        const recovery = actionOf(store, "post_recovery");
        recovery.status = recoveryStatus;
        recovery.leaseExpiresAtMs = recoveryStatus === "claimed" ? 100 : null;
        recovery.reconcileAtMs = recoveryStatus === "uncertain" ? 100 : null;
        store.putAction(recovery);
      });

      const queued = machine.applyEvent(
        envelope("firing-2", "firing", "2026-07-20T00:00:03.000Z"),
        "sha256:f2",
        3,
      );
      expect(queued).toMatchObject({
        action: "queued_next_generation",
        generation: 2,
      });
      expect(store.getGeneration(2)).toBeNull();

      completeAction(store, machine, "post_recovery", {}, 4);
      expect(store.getGeneration(1)?.state).toBe("resolved");
      expect(store.getGeneration(2)).toMatchObject({
        state: "opening",
        occurrenceCount: 1,
      });
      expect(actionOf(store, "reserve_quota", 2)).toMatchObject({
        status: "blocked",
        dependsOnActionId: actionOf(store, "release_quota", 1).actionId,
      });
      expect(actionOf(store, "post_parent", 2).status).toBe("blocked");
    },
  );

  it("opens the next generation immediately when recovery was already completed", () => {
    const { store, machine } = harness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    admitQuota(store, machine, 1);
    completeAction(store, machine, "post_parent", { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } }, 1);
    machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:02.000Z"),
      "sha256:r1",
      2,
    );
    completeAction(store, machine, "post_recovery", {}, 3);

    const next = machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:04.000Z"),
      "sha256:f2",
      4,
    );
    expect(next).toMatchObject({ action: "opened", generation: 2 });
    expect(store.getGeneration(1)?.state).toBe("resolved");
    expect(store.getGeneration(2)?.state).toBe("opening");
  });

  it("cancels a queued next generation on newer resolution without reusing its generation", () => {
    const { store, machine } = harness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    admitQuota(store, machine, 1);
    completeAction(store, machine, "post_parent", { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } }, 1);
    machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:02.000Z"),
      "sha256:r1",
      2,
    );
    store.transaction(() => {
      const recovery = actionOf(store, "post_recovery");
      recovery.status = "claimed";
      recovery.leaseExpiresAtMs = 100;
      store.putAction(recovery);
    });
    const firstCandidate = machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:03.000Z"),
      "sha256:f2",
      3,
    );
    const cancelled = machine.applyEvent(
      envelope("resolved-2", "resolved", "2026-07-20T00:00:04.000Z"),
      "sha256:r2",
      4,
    );
    const secondCandidate = machine.applyEvent(
      envelope("firing-3", "firing", "2026-07-20T00:00:05.000Z"),
      "sha256:f3",
      5,
    );

    expect(firstCandidate.generation).toBe(2);
    expect(cancelled.action).toBe("cancelled_next_generation");
    expect(secondCandidate.generation).toBe(3);
  });

  it("rolls back all synchronous state when action creation fails", () => {
    const store = new InMemoryIncidentStore();
    const machine = new IncidentStateMachine(store, {
      idFactory: (kind) => {
        if (kind === "action") throw new Error("injected action write failure");
        return `${kind}-1`;
      },
    });

    expect(() =>
      machine.applyEvent(
        envelope("event-1", "firing", "2026-07-20T00:00:00.000Z"),
        "sha256:1",
        0,
      ),
    ).toThrow("injected action write failure");
    expect(store.snapshot()).toMatchObject({
      generations: [],
      receipts: [],
      actions: [],
      analysisJobs: [],
    });
  });

  it("does not overwrite a succeeded analysis job with a late dispatch terminal result", () => {
    const { store, machine } = harness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    admitQuota(store, machine, 1);
    completeAction(store, machine, "post_parent", { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } }, 1);
    const dispatch = actionOf(store, "dispatch_analysis");
    const job = store.listAnalysisJobs()[0];
    job.status = "succeeded";
    job.version += 1;
    job.completion = { summary: "root cause confirmed" };
    job.deadlineAtMs = null;
    store.putAnalysisJob(job);

    store.transaction(() => machine.onActionTerminal(dispatch, "late timeout", 2));

    expect(store.getAnalysisJob(job.jobId)).toMatchObject({
      status: "succeeded",
      version: job.version,
      completion: { summary: "root cause confirmed" },
      lastError: null,
    });
  });
});

describe("DurableObjectSqlStore schema", () => {
  it("initializes all durable tables synchronously without transaction control SQL", () => {
    const statements: string[] = [];
    const storage = {
      sql: {
        exec(statement: string) {
          statements.push(statement);
          return [];
        },
      },
      transactionSync<T>(callback: () => T): T {
        return callback();
      },
    } as unknown as DurableObjectStorage;

    new DurableObjectSqlStore(storage).initializeSchema();

    expect(statements).toHaveLength(SQL_SCHEMA.length);
    const schema = statements.join("\n");
    for (const table of [
      "incident_generations",
      "event_receipts",
      "pending_actions",
      "analysis_jobs",
      "scheduler_state",
    ]) {
      expect(schema).toContain(table);
    }
    expect(schema).not.toMatch(/\b(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT)\b/i);
  });
});
