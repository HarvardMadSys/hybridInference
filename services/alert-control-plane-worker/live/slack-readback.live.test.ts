import { describe, expect, it } from "vitest";

import type {
  DeliveryRef,
  NotificationAction,
  NotificationActionResult,
} from "../src/notification";
import { deliveryRefEquals } from "../src/notification";
import { SlackSink } from "../src/slack";
import type { CanonicalAlertEnvelope } from "../src/types";

const CONFIRMATION = "write-synthetic-messages";
const FALLBACK_RECONCILE_DELAY_MS = 60_000;
const RECONCILE_DEADLINE_MS = 900_000;
const CLOCK_SKEW_SAFETY_MS = 250;
const MIN_WRITE_INTERVAL_MS = 1_100;
const CHANNEL_ID_RE = /^[CGD][A-Z0-9]{1,255}$/;
const SINK_ID_RE = /^[a-z][a-z0-9_-]{0,63}$/;

interface LiveConfig {
  readonly botToken: string;
  readonly channelId: string;
  readonly sinkId: string;
}

interface RuntimeProcess {
  readonly env: Readonly<Record<string, string | undefined>>;
}

interface WriteTransportSnapshot {
  readonly writeRequests: number;
  readonly droppedSuccessfulResponses: number;
}

function runtimeEnvironment(): RuntimeProcess["env"] {
  const runtime = (globalThis as typeof globalThis & { process?: RuntimeProcess }).process;
  if (runtime === undefined) throw new Error("live gate requires a Node-compatible process");
  return runtime.env;
}

function requiredEnvironmentValue(
  env: RuntimeProcess["env"],
  name: string,
): string {
  const value = env[name]?.trim();
  if (value === undefined || value.length === 0) {
    throw new Error(`live gate requires ${name}`);
  }
  return value;
}

function liveConfig(): LiveConfig {
  const env = runtimeEnvironment();
  if (env.SLACK_LIVE_GATE_CONFIRM !== CONFIRMATION) {
    throw new Error(
      `live gate requires SLACK_LIVE_GATE_CONFIRM=${CONFIRMATION}`,
    );
  }
  const botToken = requiredEnvironmentValue(env, "SLACK_BOT_TOKEN");
  const channelId = requiredEnvironmentValue(env, "SLACK_CHANNEL_ID");
  const sinkId = requiredEnvironmentValue(env, "SLACK_SINK_ID");
  if (!botToken.startsWith("xoxb-")) {
    throw new Error("SLACK_BOT_TOKEN must be a bot token");
  }
  if (!CHANNEL_ID_RE.test(channelId)) {
    throw new Error("SLACK_CHANNEL_ID must be a Slack conversation ID");
  }
  if (!SINK_ID_RE.test(sinkId)) {
    throw new Error("SLACK_SINK_ID must be a stable lowercase identifier");
  }
  return { botToken, channelId, sinkId };
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

class DropSuccessfulWriteResponses {
  private writeRequests = 0;
  private droppedSuccessfulResponses = 0;

  readonly fetch = (async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const url = new URL(requestUrl(input));
    const isWrite =
      init?.method === "POST" &&
      url.origin === "https://slack.com" &&
      (url.pathname === "/api/chat.postMessage" || url.pathname === "/api/chat.update");
    if (isWrite) this.writeRequests += 1;

    const response = await fetch(input, init);
    if (!isWrite || !response.ok) return response;

    let accepted = false;
    try {
      const body = (await response.clone().json()) as unknown;
      accepted =
        body !== null &&
        typeof body === "object" &&
        !Array.isArray(body) &&
        (body as Record<string, unknown>).ok === true;
    } catch {
      return response;
    }
    if (!accepted) return response;

    this.droppedSuccessfulResponses += 1;
    throw new Error("synthetic post-accept response drop");
  }) as typeof fetch;

  snapshot(): WriteTransportSnapshot {
    return {
      writeRequests: this.writeRequests,
      droppedSuccessfulResponses: this.droppedSuccessfulResponses,
    };
  }
}

function runId(): string {
  const random = new Uint32Array(2);
  crypto.getRandomValues(random);
  return `${Date.now().toString(36)}-${[...random]
    .map((value) => value.toString(16).padStart(8, "0"))
    .join("")}`;
}

function envelope(
  id: string,
  status: "firing" | "resolved",
  occurredAt: string,
): CanonicalAlertEnvelope {
  return {
    event: {
      schema_version: 1,
      event_id: `${id}-${status}`,
      alert_type: "provider_circuit_open",
      fingerprint: `slack-live-gate:${id}`,
      status,
      severity: "warn",
      title: `[LIVE GATE] Synthetic alert ${id}`,
      occurred_at: occurredAt,
      summary:
        status === "firing"
          ? "Synthetic alert used to verify Slack metadata reconciliation."
          : "Synthetic alert recovery used to verify threaded reconciliation.",
      context: {
        provider: `slack-live-gate:${id}`,
        availability: status === "firing" ? 0 : 1,
        reason: status === "firing" ? "availability_below_threshold" : "unknown",
        ...(status === "resolved"
          ? { final_failure_count: 2, outage_duration_ms: 1_000 }
          : { consecutive_failures: 2 }),
      },
      evidence_refs: ["live/slack-readback.live.test.ts"],
    },
    trusted: {
      environment: "staging",
      target_environment: "staging",
      source: "gateway",
      principal: "slack-live-gate",
      deployment_id: "slack-live-gate",
      deployment_sha: "a".repeat(40),
      artifact_digest: `sha256:${"b".repeat(64)}`,
      registry_version: 1,
    },
  };
}

async function digestPayload(payload: Readonly<Record<string, unknown>>): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hex = [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  return `sha256:${hex}`;
}

async function notificationAction(
  type: NotificationAction["type"],
  id: string,
  sinkId: string,
  deliveryRef: DeliveryRef | null,
  firstSeen: string,
  occurredAt: string,
): Promise<NotificationAction> {
  const payload =
    type === "post_analysis"
      ? {
          incident_id: `slack-live-${id}`,
          generation: 1,
          analysis: {
            summary: "Synthetic live-gate analysis reply.",
            run_id: id,
          },
        }
      : {
          incident_id: `slack-live-${id}`,
          generation: 1,
          occurrence_count: type === "post_parent" ? 1 : 2,
          first_seen: firstSeen,
          last_seen: occurredAt,
          envelope: envelope(id, type === "post_recovery" ? "resolved" : "firing", occurredAt),
        };
  return {
    type,
    actionId: `${id}-${type}`,
    sinkId,
    incidentId: `slack-live-${id}`,
    generation: 1,
    attemptStartedAtMs: Date.now(),
    payloadDigest: await digestPayload(payload),
    deliveryRef,
    payload,
  };
}

function expectUncertainWrite(result: NotificationActionResult): void {
  expect(result).toEqual({
    outcome: "uncertain",
    errorCode: "slack_network_uncertain",
  });
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function reconcileUntilSuccess(
  sink: SlackSink,
  action: NotificationAction,
): Promise<Extract<NotificationActionResult, { outcome: "success" }>> {
  const deadline = Date.now() + RECONCILE_DEADLINE_MS;
  for (;;) {
    const result = await sink.execute(action, "reconcile");
    if (result.outcome === "success") return result;
    if (result.outcome !== "uncertain") {
      throw new Error(`reconciliation stopped with ${result.outcome}:${result.errorCode}`);
    }

    const now = Date.now();
    const nextAttemptAt =
      result.reconcileAtMs ?? now + FALLBACK_RECONCILE_DELAY_MS;
    const wakeAt = Math.max(now + CLOCK_SKEW_SAFETY_MS, nextAttemptAt + CLOCK_SKEW_SAFETY_MS);
    if (wakeAt > deadline) throw new Error("reconciliation exceeded the live-gate deadline");
    await sleep(wakeAt - now);
  }
}

async function writeThenReconcile(
  sink: SlackSink,
  transport: DropSuccessfulWriteResponses,
  action: NotificationAction,
): Promise<Extract<NotificationActionResult, { outcome: "success" }>> {
  const before = transport.snapshot();
  expectUncertainWrite(await sink.execute(action, "execute"));
  const afterWrite = transport.snapshot();
  expect(afterWrite).toEqual({
    writeRequests: before.writeRequests + 1,
    droppedSuccessfulResponses: before.droppedSuccessfulResponses + 1,
  });

  const result = await reconcileUntilSuccess(sink, action);
  expect(transport.snapshot()).toEqual(afterWrite);
  return result;
}

describe.sequential("Slack target-workspace metadata readback gate", () => {
  it("reconciles every deliberately ambiguous write without replay", async () => {
    const config = liveConfig();
    const id = runId();
    const transport = new DropSuccessfulWriteResponses();
    const sink = new SlackSink({
      sinkId: config.sinkId,
      botToken: config.botToken,
      channelId: config.channelId,
      fetch: transport.fetch,
    });

    const firstSeen = new Date().toISOString();
    const parentAction = await notificationAction(
      "post_parent",
      id,
      config.sinkId,
      null,
      firstSeen,
      firstSeen,
    );
    const parent = await writeThenReconcile(sink, transport, parentAction);
    const afterParent = transport.snapshot();
    const parentAgain = await reconcileUntilSuccess(sink, parentAction);
    expect(transport.snapshot()).toEqual(afterParent);
    expect(deliveryRefEquals(parent.receipt.deliveryRef, parentAgain.receipt.deliveryRef)).toBe(
      true,
    );
    expect(parent.receipt.deliveryRef.destinationId).toBe(config.channelId);
    expect(parent.receipt.deliveryRef.messageId).toBe(
      parent.receipt.deliveryRef.conversationId,
    );

    await sleep(MIN_WRITE_INTERVAL_MS);
    const updateAt = new Date().toISOString();
    const updateAction = await notificationAction(
      "update_parent",
      id,
      config.sinkId,
      parent.receipt.deliveryRef,
      firstSeen,
      updateAt,
    );
    const update = await writeThenReconcile(sink, transport, updateAction);
    expect(deliveryRefEquals(update.receipt.deliveryRef, parent.receipt.deliveryRef)).toBe(true);
    expect(update.receipt.externalEffectId).toBe(parent.receipt.deliveryRef.messageId);

    await sleep(MIN_WRITE_INTERVAL_MS);
    const recoveryAt = new Date().toISOString();
    const recoveryAction = await notificationAction(
      "post_recovery",
      id,
      config.sinkId,
      parent.receipt.deliveryRef,
      firstSeen,
      recoveryAt,
    );
    const recovery = await writeThenReconcile(sink, transport, recoveryAction);
    expect(deliveryRefEquals(recovery.receipt.deliveryRef, parent.receipt.deliveryRef)).toBe(true);
    expect(recovery.receipt.externalEffectId).toBeDefined();
    expect(recovery.receipt.externalEffectId).not.toBe(parent.receipt.deliveryRef.messageId);

    await sleep(MIN_WRITE_INTERVAL_MS);
    const analysisAction = await notificationAction(
      "post_analysis",
      id,
      config.sinkId,
      parent.receipt.deliveryRef,
      firstSeen,
      new Date().toISOString(),
    );
    const analysis = await writeThenReconcile(sink, transport, analysisAction);
    expect(deliveryRefEquals(analysis.receipt.deliveryRef, parent.receipt.deliveryRef)).toBe(true);
    expect(analysis.receipt.externalEffectId).toBeDefined();
    expect(analysis.receipt.externalEffectId).not.toBe(parent.receipt.deliveryRef.messageId);
    expect(analysis.receipt.externalEffectId).not.toBe(recovery.receipt.externalEffectId);
    expect(transport.snapshot()).toEqual({
      writeRequests: 4,
      droppedSuccessfulResponses: 4,
    });

    console.info(
      JSON.stringify({
        gate: "slack_target_workspace_readback_passed",
        runId: id,
        sinkId: config.sinkId,
        channelId: config.channelId,
        parentMessageId: parent.receipt.deliveryRef.messageId,
        updateMessageId: update.receipt.externalEffectId,
        recoveryMessageId: recovery.receipt.externalEffectId,
        analysisMessageId: analysis.receipt.externalEffectId,
      }),
    );
  });
});
