import { describe, expect, it } from "vitest";

import notificationSource from "../src/notification.ts?raw";
import type { NotificationActionResult, NotificationSink } from "../src/notification";
import {
  NotificationActionExecutor,
  projectNotificationAction,
  serializeNotificationResult,
} from "../src/notification-executor";
import type { ActionClaim } from "../src/outbox";
import { InMemoryIncidentStore } from "../src/store";
import {
  deliveryRef,
  incidentGeneration,
  pendingAction,
  ThreadedFakeSink,
} from "./fakes";

function claim(action: ActionClaim["action"], mode: ActionClaim["mode"] = "execute"): ActionClaim {
  return { action, mode };
}

describe("projectNotificationAction", () => {
  it("forwards only allowlisted semantic fields and drops fence/platform fields", async () => {
    const action = pendingAction({
      type: "post_recovery",
      payload: {
        incident_id: "incident-1",
        generation: 1,
        occurrence_count: 3,
        envelope: { event: { event_id: "e1" } },
        first_seen: "2026-07-20T00:00:00.000Z",
        last_seen: "2026-07-20T00:05:00.000Z",
        state_version: 9,
        resolution_epoch: 4,
        delivery_ref: deliveryRef(),
        slack_thread_ts: "100.001",
      },
    });

    const projected = await projectNotificationAction(action, deliveryRef(), "slack-primary");

    expect(Object.keys(projected.payload).sort()).toEqual([
      "envelope",
      "first_seen",
      "generation",
      "incident_id",
      "last_seen",
      "occurrence_count",
    ]);
    expect(projected.payload).not.toHaveProperty("state_version");
    expect(projected.payload).not.toHaveProperty("resolution_epoch");
    expect(projected.payload).not.toHaveProperty("delivery_ref");
    expect(projected.payload).not.toHaveProperty("slack_thread_ts");
    expect(projected.deliveryRef).toEqual(deliveryRef());
    expect(projected.sinkId).toBe("slack-primary");
    expect(projected.payloadDigest).toMatch(/^sha256:[0-9a-f]{64}$/);
  });

  it("keeps structured analysis for post_analysis while still dropping fences", async () => {
    const action = pendingAction({
      type: "post_analysis",
      payload: {
        incident_id: "incident-1",
        generation: 1,
        analysis: { summary: "root cause", confidence: "high" },
        state_version: 2,
      },
    });

    const projected = await projectNotificationAction(action, deliveryRef(), "slack-primary");

    expect(projected.payload).toEqual({
      incident_id: "incident-1",
      generation: 1,
      analysis: { summary: "root cause", confidence: "high" },
    });
  });

  it("produces a stable digest for identical inputs and a different one otherwise", async () => {
    const action = pendingAction({ type: "update_parent", payload: { incident_id: "a", generation: 1 } });
    const other = pendingAction({ type: "update_parent", payload: { incident_id: "b", generation: 1 } });

    const first = await projectNotificationAction(action, deliveryRef(), "slack-primary");
    const same = await projectNotificationAction(action, deliveryRef(), "slack-primary");
    const different = await projectNotificationAction(other, deliveryRef(), "slack-primary");

    expect(first.payloadDigest).toBe(same.payloadDigest);
    expect(first.payloadDigest).not.toBe(different.payloadDigest);
  });

  it("requires a null delivery reference for post_parent and a non-null one otherwise", async () => {
    await expect(
      projectNotificationAction(pendingAction({ type: "post_parent" }), deliveryRef(), "slack-primary"),
    ).rejects.toThrow(/post_parent must be projected with a null/);

    const parent = await projectNotificationAction(
      pendingAction({ type: "post_parent" }),
      null,
      "slack-primary",
    );
    expect(parent.deliveryRef).toBeNull();

    for (const type of ["update_parent", "post_recovery", "post_analysis"] as const) {
      await expect(
        projectNotificationAction(pendingAction({ type }), null, "slack-primary"),
      ).rejects.toThrow(/requires an existing delivery reference/);
    }
  });

  it("rejects a non-notification action type", async () => {
    await expect(
      projectNotificationAction(pendingAction({ type: "reserve_quota" }), null, "slack-primary"),
    ).rejects.toThrow(/non-notification action/);
  });

  it("rejects a delivery reference whose sinkId does not match the target sink", async () => {
    await expect(
      projectNotificationAction(
        pendingAction({ type: "update_parent" }),
        deliveryRef({ sinkId: "other-sink" }),
        "slack-primary",
      ),
    ).rejects.toThrow(/sinkId does not match/);
  });
});

describe("serializeNotificationResult", () => {
  it("maps every notification outcome onto the outbox result union", () => {
    const receipt = { deliveryRef: deliveryRef(), externalEffectId: "reply-1" };

    expect(serializeNotificationResult({ outcome: "success", receipt })).toEqual({
      outcome: "success",
      result: { receipt },
    });
    expect(
      serializeNotificationResult({ outcome: "retry", errorCode: "rate_limited", retryAtMs: 42 }),
    ).toEqual({ outcome: "retry", error: "rate_limited", retryAtMs: 42 });
    expect(
      serializeNotificationResult({ outcome: "uncertain", errorCode: "timeout", reconcileAtMs: 7 }),
    ).toEqual({ outcome: "uncertain", error: "timeout", reconcileAtMs: 7 });
    expect(
      serializeNotificationResult({
        outcome: "manual_reconciliation_required",
        errorCode: "ambiguous",
      }),
    ).toEqual({ outcome: "manual_reconciliation_required", error: "ambiguous" });
    expect(serializeNotificationResult({ outcome: "failed", errorCode: "auth" })).toEqual({
      outcome: "failed",
      error: "auth",
    });
  });

  it("persists the normalized receipt rather than the sink-owned object", () => {
    const sinkReceipt = {
      deliveryRef: {
        ...deliveryRef(),
        conversationId: undefined,
      },
      externalEffectId: "1620000001.000200",
    };

    expect(serializeNotificationResult({ outcome: "success", receipt: sinkReceipt })).toEqual({
      outcome: "success",
      result: {
        receipt: {
          deliveryRef: {
            schemaVersion: 1,
            sinkId: "slack-primary",
            platform: "slack",
            destinationId: "C123",
            messageId: "100.001",
          },
          externalEffectId: "1620000001.000200",
        },
      },
    });
  });

  it("replaces a non-stable error code so response bodies cannot be persisted", () => {
    for (const errorCode of [
      "Rate limited: retry after 30s",
      "https://slack.com/api/chat.postMessage?token=xoxb-secret",
      "UPPER_CASE",
      "x".repeat(65),
      "",
    ]) {
      expect(serializeNotificationResult({ outcome: "failed", errorCode })).toEqual({
        outcome: "failed",
        error: "sink_error_unclassified",
      });
    }
    // A well-formed stable code is preserved verbatim.
    expect(
      serializeNotificationResult({ outcome: "retry", errorCode: "slack_rate_limited" }),
    ).toMatchObject({ error: "slack_rate_limited" });
  });

  it.each([
    [
      "an unsafe external effect ID",
      {
        deliveryRef: deliveryRef(),
        externalEffectId: "xoxb-1234567890abcdef",
      },
      "receipt_external_effect_id_invalid",
    ],
    [
      "an unknown receipt field",
      {
        deliveryRef: deliveryRef(),
        responseBody: "ok",
      },
      "receipt_invalid",
    ],
  ])("does not serialize a success receipt with %s", (_label, receipt, error) => {
    expect(
      serializeNotificationResult({
        outcome: "success",
        receipt,
      } as unknown as NotificationActionResult),
    ).toEqual({
      outcome: "manual_reconciliation_required",
      error,
    });
  });
});

describe("NotificationActionExecutor", () => {
  it("throws for a non-notification action", async () => {
    const executor = new NotificationActionExecutor(new InMemoryIncidentStore(), new ThreadedFakeSink());
    await expect(
      executor.execute(claim(pendingAction({ type: "reserve_quota" }))),
    ).rejects.toThrow(/received non-notification action/);
  });

  it("surfaces a sink failure as a failed outbox result", async () => {
    const failingSink = {
      sinkId: "slack-primary",
      platform: "slack" as const,
      async execute(): Promise<NotificationActionResult> {
        return { outcome: "failed", errorCode: "auth_error" };
      },
    };
    const executor = new NotificationActionExecutor(new InMemoryIncidentStore(), failingSink);

    await expect(
      executor.execute(claim(pendingAction({ type: "post_parent", generation: 1 }))),
    ).resolves.toEqual({ outcome: "failed", error: "auth_error" });
  });

  it("surfaces a sinkId mismatch as an invariant violation before touching the sink", async () => {
    const store = new InMemoryIncidentStore();
    store.putGeneration(
      incidentGeneration({ generation: 1, deliveryRef: deliveryRef({ sinkId: "other-sink" }) }),
    );
    const sink = new ThreadedFakeSink();
    const executor = new NotificationActionExecutor(store, sink);

    await expect(
      executor.execute(claim(pendingAction({ type: "update_parent", generation: 1 }))),
    ).rejects.toThrow(/sinkId does not match/);
    expect(sink.calls).toHaveLength(0);
  });

  it("reads the authoritative delivery reference from the store for replies", async () => {
    const store = new InMemoryIncidentStore();
    store.putGeneration(
      incidentGeneration({ generation: 1, deliveryRef: deliveryRef({ messageId: "100.001" }) }),
    );
    const sink = new ThreadedFakeSink();
    const executor = new NotificationActionExecutor(store, sink);

    const result = await executor.execute(
      claim(pendingAction({ type: "post_recovery", generation: 1 })),
    );

    expect(result).toMatchObject({ outcome: "success" });
    expect(sink.calls[0].action.deliveryRef).toEqual(deliveryRef({ messageId: "100.001" }));
  });

  function sinkReturning(receiptRef: unknown): NotificationSink {
    return {
      sinkId: "slack-primary",
      platform: "slack",
      async execute(): Promise<NotificationActionResult> {
        return {
          outcome: "success",
          receipt: { deliveryRef: receiptRef as never },
        };
      },
    };
  }

  function sinkReturningReceipt(receipt: unknown): NotificationSink {
    return {
      sinkId: "slack-primary",
      platform: "slack",
      async execute(): Promise<NotificationActionResult> {
        return {
          outcome: "success",
          receipt: receipt as never,
        };
      },
    };
  }

  it("rejects a post_parent receipt bound to a different sink", async () => {
    const executor = new NotificationActionExecutor(
      new InMemoryIncidentStore(),
      sinkReturning(deliveryRef({ sinkId: "other-sink" })),
    );
    await expect(
      executor.execute(claim(pendingAction({ type: "post_parent", generation: 1 }))),
    ).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      error: "receipt_sink_mismatch",
    });
  });

  it("rejects a recovery receipt that mutates the parent reference and cannot resolve", async () => {
    const store = new InMemoryIncidentStore();
    store.putGeneration(
      incidentGeneration({ generation: 1, deliveryRef: deliveryRef({ messageId: "100.001" }) }),
    );
    const executor = new NotificationActionExecutor(
      store,
      sinkReturning(deliveryRef({ messageId: "999.999" })),
    );
    await expect(
      executor.execute(claim(pendingAction({ type: "post_recovery", generation: 1 }))),
    ).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      error: "receipt_reference_mismatch",
    });
  });

  it("rejects a structurally invalid or secret-bearing receipt reference", async () => {
    const executor = new NotificationActionExecutor(
      new InMemoryIncidentStore(),
      sinkReturning(deliveryRef({ messageId: "https://hooks.slack.com/services/x" })),
    );
    await expect(
      executor.execute(claim(pendingAction({ type: "post_parent", generation: 1 }))),
    ).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      error: "receipt_reference_invalid",
    });
  });

  it("rejects an unsafe externalEffectId before it reaches the outbox result", async () => {
    const executor = new NotificationActionExecutor(
      new InMemoryIncidentStore(),
      sinkReturningReceipt({
        deliveryRef: deliveryRef(),
        externalEffectId: "api_key=NOTAREAL",
      }),
    );

    await expect(
      executor.execute(claim(pendingAction({ type: "post_parent", generation: 1 }))),
    ).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      error: "receipt_external_effect_id_invalid",
    });
  });
});

describe("notification contract import boundary", () => {
  it("does not import store, incident, outbox, render, or index", () => {
    // Guard against ?raw silently yielding empty content and passing vacuously.
    expect(notificationSource).toContain("export function parseDeliveryRef");
    expect(notificationSource).not.toMatch(
      /from\s+["']\.\/(?:store|incident|outbox|render|index)["']/,
    );
    // The pure contract should have no relative imports at all.
    expect(notificationSource).not.toMatch(/\bfrom\s+["']\.\.?\//);
  });
});
