import { describe, expect, it, vi } from "vitest";

import { IncidentStateMachine } from "../src/incident";
import { OutboxRunner } from "../src/outbox";
import {
  InMemoryIncidentStore,
  type AnalysisJob,
  type PendingAction,
} from "../src/store";
import type { DeliveryRef } from "../src/notification";
import {
  deferred,
  deliveryRef,
  deterministicIds,
  envelope,
  pendingAction,
  ScriptedExecutor,
} from "./fakes";

function lifecycleHarness(executor = new ScriptedExecutor()) {
  const store = new InMemoryIncidentStore();
  const machine = new IncidentStateMachine(store, {
    idFactory: deterministicIds(),
    repeatUpdateDelayMs: 10,
    actionDeadlineMs: 1_000,
    analysisDeadlineMs: 500,
  });
  const runner = new OutboxRunner(store, executor, {
    claimLeaseMs: 10,
    retryBaseDelayMs: 5,
    reconcileDelayMs: 5,
    hooks: {
      onActionCompleted: machine.onActionCompleted.bind(machine),
      onActionTerminal: machine.onActionTerminal.bind(machine),
    },
  });
  return { store, machine, runner, executor };
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

const quotaAdmission = {
  outcome: "success" as const,
  result: { admitted: true, lease_epoch: 1 },
};

describe("OutboxRunner", () => {
  it("persists the minimum action, lease, reconciliation, and analysis deadline", () => {
    const store = new InMemoryIncidentStore();
    store.putAction(pendingAction({ actionId: "pending", nextRunAtMs: 100 }));
    store.putAction(
      pendingAction({
        actionId: "claimed",
        status: "claimed",
        nextRunAtMs: null,
        leaseExpiresAtMs: 60,
      }),
    );
    store.putAction(
      pendingAction({
        actionId: "uncertain",
        status: "uncertain",
        nextRunAtMs: null,
        reconcileAtMs: 80,
      }),
    );
    store.putAction(
      pendingAction({
        actionId: "blocked",
        status: "blocked",
        nextRunAtMs: 1,
        dependsOnActionId: "pending",
      }),
    );
    const job: AnalysisJob = {
      jobId: "job-1",
      incidentId: "incident-1",
      generation: 1,
      version: 1,
      status: "queued",
      checkoutSha: "a".repeat(40),
      executionLeaseNonce: null,
      githubRunId: null,
      githubRunAttempt: null,
      leaseExpiresAtMs: null,
      deadlineAtMs: 40,
      completion: null,
      lastError: null,
      createdAtMs: 0,
      updatedAtMs: 0,
    };
    store.putAnalysisJob(job);
    const runner = new OutboxRunner(store, new ScriptedExecutor());

    expect(runner.desiredAlarmAt()).toBe(40);
    expect(store.getSchedulerState()).toMatchObject({ desiredAlarmAtMs: 40 });

    job.status = "failed";
    job.deadlineAtMs = null;
    store.putAnalysisJob(job);
    expect(runner.desiredAlarmAt()).toBe(60);
  });

  it("commits parent result and incident activation atomically, then queues analysis", async () => {
    const { store, machine, runner, executor } = lifecycleHarness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    executor.enqueue(
      quotaAdmission,
      { outcome: "success", result: { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } } },
    );

    await expect(runner.runOne(0)).resolves.toMatchObject({
      ran: true,
      mode: "execute",
      outcome: "success",
    });
    expect(executor.claims[0].action.type).toBe("reserve_quota");
    await runner.runOne(0);

    expect(actionOf(store, "post_parent")).toMatchObject({
      status: "completed",
      claimEpoch: 1,
    });
    expect(store.getGeneration(1)).toMatchObject({
      state: "firing",
      deliveryRef: deliveryRef({ messageId: "100.001" }),
    });
    expect(store.listAnalysisJobs()[0]).toMatchObject({ status: "queued", deadlineAtMs: 500 });
    expect(actionOf(store, "dispatch_analysis").status).toBe("pending");
    expect(store.getSchedulerState().desiredAlarmAtMs).toBe(0);
  });

  it("suppresses the generation when principal quota denies admission", async () => {
    const { store, machine, runner, executor } = lifecycleHarness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    executor.enqueue({ outcome: "success", result: { admitted: false } });

    await runner.runOne(0);

    expect(executor.claims.map((claim) => claim.action.type)).toEqual([
      "reserve_quota",
    ]);
    expect(store.getGeneration(1)).toMatchObject({
      state: "suppressed",
      quotaState: "suppressed",
    });
    expect(actionOf(store, "post_parent").status).toBe("cancelled");
    await expect(runner.runOne(1)).resolves.toMatchObject({
      ran: false,
      outcome: "idle",
    });
  });

  it("keeps parent creation before recovery when resolved arrives during opening", async () => {
    const { store, machine, runner, executor } = lifecycleHarness();
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
    expect(actionOf(store, "post_recovery").status).toBe("blocked");
    executor.enqueue(
      quotaAdmission,
      { outcome: "success", result: { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } } },
      {
        outcome: "success",
        result: {
          receipt: {
            deliveryRef: deliveryRef({ messageId: "100.001" }),
            externalEffectId: "100.002",
          },
        },
      },
    );

    await runner.runOne(2);
    expect(executor.claims[0].action.type).toBe("reserve_quota");
    await runner.runOne(2);
    expect(executor.claims[1].action.type).toBe("post_parent");
    expect(actionOf(store, "post_recovery")).toMatchObject({
      status: "pending",
      dependsOnActionId: null,
    });

    await runner.runOne(2);
    expect(executor.claims[2].action.type).toBe("post_recovery");
    expect(store.getGeneration(1)?.state).toBe("resolved");
  });

  it("backs off only a definitively retryable failure and stops at the attempt bound", async () => {
    const store = new InMemoryIncidentStore();
    store.putAction(pendingAction({ nextRunAtMs: 0 }));
    const executor = new ScriptedExecutor();
    executor.enqueue(
      { outcome: "retry", error: "request was rejected before acceptance" },
      { outcome: "retry", error: "still rejected" },
    );
    const terminal = vi.fn();
    const runner = new OutboxRunner(store, executor, {
      retryBaseDelayMs: 5,
      maxAttempts: 2,
      hooks: { onActionTerminal: terminal },
    });

    await runner.runOne(0);
    expect(store.getAction("action-1")).toMatchObject({
      status: "pending",
      attempt: 1,
      nextRunAtMs: 5,
    });
    await runner.runOne(5);
    expect(store.getAction("action-1")).toMatchObject({
      status: "failed",
      attempt: 2,
      nextRunAtMs: null,
    });
    expect(terminal).toHaveBeenCalledOnce();
  });

  it("treats an executor exception as ambiguous and reconciles instead of reposting", async () => {
    const store = new InMemoryIncidentStore();
    store.putAction(pendingAction({ nextRunAtMs: 0 }));
    const executor = new ScriptedExecutor();
    executor.enqueue(
      new Error("response was lost"),
      { outcome: "success", result: { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } } },
    );
    const runner = new OutboxRunner(store, executor, { reconcileDelayMs: 5 });

    await runner.runOne(0);
    expect(store.getAction("action-1")).toMatchObject({
      status: "uncertain",
      attempt: 1,
      reconcileAtMs: 5,
    });
    await expect(runner.runOne(4)).resolves.toMatchObject({ ran: false, outcome: "idle" });
    await runner.runOne(5);

    expect(executor.claims.map((claim) => claim.mode)).toEqual(["execute", "reconcile"]);
    expect(store.getAction("action-1")).toMatchObject({
      status: "completed",
      attempt: 1,
      claimEpoch: 2,
    });
  });

  it("reclaims an expired lease for reconciliation and fences the old worker result", async () => {
    const store = new InMemoryIncidentStore();
    store.putAction(pendingAction({ nextRunAtMs: 0 }));
    const first = deferred<{
      outcome: "success";
      result: { receipt: { deliveryRef: DeliveryRef } };
    }>();
    const second = deferred<{
      outcome: "success";
      result: { receipt: { deliveryRef: DeliveryRef } };
    }>();
    const executor = new ScriptedExecutor();
    executor.enqueue(first.promise, second.promise);
    const runner = new OutboxRunner(store, executor, { claimLeaseMs: 10 });

    const oldWorker = runner.runOne(0);
    expect(store.getAction("action-1")).toMatchObject({
      status: "claimed",
      claimEpoch: 1,
      leaseExpiresAtMs: 10,
    });
    const reconciler = runner.runOne(11);
    expect(executor.claims.map((claim) => claim.mode)).toEqual(["execute", "reconcile"]);

    first.resolve({
      outcome: "success",
      result: { receipt: { deliveryRef: deliveryRef({ messageId: "wrong-late-ts" }) } },
    });
    await expect(oldWorker).resolves.toMatchObject({ outcome: "stale_result" });
    expect(store.getAction("action-1")?.status).toBe("claimed");

    second.resolve({
      outcome: "success",
      result: { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } },
    });
    await expect(reconciler).resolves.toMatchObject({ outcome: "success" });
    expect(store.getAction("action-1")).toMatchObject({
      status: "completed",
      claimEpoch: 2,
      result: { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } },
    });
  });

  it("rolls back lifecycle completion and fences the action uncertain if completion hook fails", async () => {
    const executor = new ScriptedExecutor();
    const { store, machine, runner: admissionRunner } = lifecycleHarness(executor);
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    executor.enqueue(quotaAdmission);
    await admissionRunner.runOne(0);
    executor.enqueue({
      outcome: "success",
      result: { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } },
    });
    const runner = new OutboxRunner(store, executor, {
      reconcileDelayMs: 5,
      hooks: {
        onActionCompleted(action, result, nowMs) {
          machine.onActionCompleted(action, result, nowMs);
          throw new Error("injected lifecycle commit failure");
        },
      },
    });

    await expect(runner.runOne(0)).rejects.toThrow("injected lifecycle commit failure");
    expect(store.getGeneration(1)).toMatchObject({
      state: "opening",
      deliveryRef: null,
    });
    expect(actionOf(store, "post_parent")).toMatchObject({
      status: "uncertain",
      reconcileAtMs: 5,
    });
  });

  it("cancels an unclaimed dispatch when its analysis deadline expires", async () => {
    const { store, machine, runner, executor } = lifecycleHarness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    executor.enqueue(
      quotaAdmission,
      { outcome: "success", result: { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } } },
    );
    await runner.runOne(0);
    await runner.runOne(0);
    expect(actionOf(store, "dispatch_analysis").status).toBe("pending");

    await expect(runner.runOne(500)).resolves.toMatchObject({
      ran: false,
      outcome: "idle",
    });

    expect(executor.claims).toHaveLength(2);
    expect(store.listAnalysisJobs()[0]).toMatchObject({
      status: "failed",
      deadlineAtMs: null,
      lastError: "analysis deadline expired",
    });
    expect(actionOf(store, "dispatch_analysis")).toMatchObject({
      status: "cancelled",
      nextRunAtMs: null,
      lastError: "analysis deadline expired before dispatch claim",
    });
    expect(runner.desiredAlarmAt()).toBeNull();
  });

  it("freezes a claimed recovery, then opens the queued generation only after recovery success", async () => {
    const { store, machine, runner, executor } = lifecycleHarness();
    machine.applyEvent(
      envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z"),
      "sha256:f1",
      0,
    );
    executor.enqueue(
      quotaAdmission,
      { outcome: "success", result: { receipt: { deliveryRef: deliveryRef({ messageId: "100.001" }) } } },
    );
    await runner.runOne(0);
    await runner.runOne(0);
    executor.enqueue({ outcome: "success", result: { githubRunId: "run-1" } });
    await runner.runOne(0);
    machine.applyEvent(
      envelope("resolved-1", "resolved", "2026-07-20T00:00:01.000Z"),
      "sha256:r1",
      1,
    );
    const recovery = deferred<{ outcome: "success"; result: Record<string, unknown> }>();
    executor.enqueue(recovery.promise);
    const recoveryRun = runner.runOne(1);
    expect(actionOf(store, "post_recovery").status).toBe("claimed");

    const queued = machine.applyEvent(
      envelope("firing-2", "firing", "2026-07-20T00:00:02.000Z"),
      "sha256:f2",
      2,
    );
    expect(queued.action).toBe("queued_next_generation");
    expect(store.getGeneration(2)).toBeNull();

    recovery.resolve({ outcome: "success", result: {} });
    await recoveryRun;
    expect(store.getGeneration(1)?.state).toBe("resolved");
    expect(store.getGeneration(2)?.state).toBe("opening");
    expect(actionOf(store, "reserve_quota", 2)).toMatchObject({
      status: "blocked",
      dependsOnActionId: actionOf(store, "release_quota", 1).actionId,
    });
    expect(actionOf(store, "post_parent", 2).status).toBe("blocked");
  });
});
