import { describe, expect, it } from "vitest";

import type {
  DeliveryRef,
  NotificationAction,
  NotificationActionResult,
} from "../src/notification";
import incidentSource from "../src/incident.ts?raw";
import {
  NotificationActionExecutor,
  projectNotificationAction,
} from "../src/notification-executor";
import { OutboxRunner } from "../src/outbox";
import slackSource from "../src/slack.ts?raw";
import { SlackSink } from "../src/slack";
import { InMemoryIncidentStore } from "../src/store";
import {
  deliveryRef,
  envelope,
  incidentGeneration,
  pendingAction,
} from "./fakes";

const START_MS = Date.parse("2026-07-20T00:00:00.000Z");
const NOW_MS = START_MS + 10_000;
const DIGEST = `sha256:${"d".repeat(64)}`;
const BOT_TOKEN = ["xoxb", "unit", "test"].join("-");

interface FetchCall {
  readonly url: string;
  readonly init: RequestInit | undefined;
}

function apiResponse(body: unknown, status = 200, headers?: HeadersInit): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function scriptedFetch(...steps: readonly (Response | Error)[]): {
  readonly fetch: typeof fetch;
  readonly calls: FetchCall[];
} {
  const queue = [...steps];
  const calls: FetchCall[] = [];
  const fake = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    calls.push({ url, init });
    const next = queue.shift();
    if (next === undefined) throw new Error("unexpected fetch");
    if (next instanceof Error) throw next;
    return next;
  };
  return { fetch: fake as typeof fetch, calls };
}

function slackTs(ms: number): string {
  return (ms / 1_000).toFixed(6);
}

function currentRef(overrides: Partial<DeliveryRef> = {}): DeliveryRef {
  const root = slackTs(START_MS - 60_000);
  return deliveryRef({ messageId: root, conversationId: root, ...overrides });
}

function action(
  type: NotificationAction["type"],
  overrides: Partial<NotificationAction> = {},
): NotificationAction {
  const firing = envelope("firing-1", "firing", "2026-07-20T00:00:00.000Z");
  const resolved = envelope("resolved-1", "resolved", "2026-07-20T00:00:05.000Z");
  const ref = type === "post_parent" ? null : currentRef();
  return {
    type,
    actionId: `action-${type}`,
    sinkId: "slack-primary",
    incidentId: "incident-1",
    generation: 1,
    attemptStartedAtMs: START_MS,
    payloadDigest: DIGEST,
    deliveryRef: ref,
    payload:
      type === "post_analysis"
        ? {
            incident_id: "incident-1",
            generation: 1,
            analysis: { summary: "Root cause confirmed" },
          }
        : {
            incident_id: "incident-1",
            generation: 1,
            occurrence_count: 2,
            first_seen: "2026-07-20T00:00:00.000Z",
            last_seen: "2026-07-20T00:00:05.000Z",
            envelope: type === "post_recovery" ? resolved : firing,
          },
    ...overrides,
  };
}

function metadataFor(target: NotificationAction, digest = target.payloadDigest): unknown {
  return {
    event_type: "alert_control_plane_action",
    event_payload: {
      action_id: target.actionId,
      incident_id: target.incidentId,
      generation: target.generation,
      payload_digest: digest,
    },
  };
}

function sinkWith(
  fetchImpl: typeof fetch,
  now = NOW_MS,
  overrides: Partial<ConstructorParameters<typeof SlackSink>[0]> = {},
): SlackSink {
  return new SlackSink({
    sinkId: "slack-primary",
    botToken: BOT_TOKEN,
    channelId: "C123",
    fetch: fetchImpl,
    now: () => now,
    reconciliationGraceMs: 5_000,
    reconciliationWindowBeforeMs: 1_000,
    reconciliationWindowAfterMs: 1_000,
    ...overrides,
  });
}

function expectSuccess(result: NotificationActionResult): asserts result is Extract<
  NotificationActionResult,
  { outcome: "success" }
> {
  expect(result.outcome).toBe("success");
  if (result.outcome !== "success") throw new Error("expected success");
}

describe("SlackSink execute", () => {
  it.each([
    ["post_parent", "chat.postMessage"],
    ["update_parent", "chat.update"],
    ["post_recovery", "chat.postMessage"],
    ["post_analysis", "chat.postMessage"],
  ] as const)("executes %s with complete metadata", async (type, method) => {
    const target = action(type);
    const responseTs = type === "update_parent" ? target.deliveryRef!.messageId : slackTs(START_MS + 1_000);
    const transport = scriptedFetch(
      apiResponse({ ok: true, channel: "C123", ts: responseTs }),
    );

    const result = await sinkWith(transport.fetch).execute(target, "execute");

    expectSuccess(result);
    expect(transport.calls).toHaveLength(1);
    expect(transport.calls[0].url).toBe(`https://slack.com/api/${method}`);
    const body = JSON.parse(String(transport.calls[0].init?.body)) as Record<string, unknown>;
    expect(body.channel).toBe("C123");
    expect(body.metadata).toEqual(metadataFor(target));
    expect(transport.calls[0].init?.headers).toEqual(
      expect.objectContaining({ authorization: `Bearer ${BOT_TOKEN}` }),
    );
    if (type === "post_parent") {
      expect(result.receipt.deliveryRef).toEqual({
        schemaVersion: 1,
        sinkId: "slack-primary",
        platform: "slack",
        destinationId: "C123",
        messageId: responseTs,
        conversationId: responseTs,
      });
    } else {
      expect(result.receipt.deliveryRef).toEqual(target.deliveryRef);
    }
    if (type === "update_parent") expect(body.ts).toBe(target.deliveryRef!.messageId);
    if (type === "post_recovery" || type === "post_analysis") {
      expect(body.thread_ts).toBe(target.deliveryRef!.conversationId);
    }
  });

  it("classifies rate limits, ambiguous failures, and explicit rejection conservatively", async () => {
    const cases: readonly [Response | Error, NotificationActionResult["outcome"], string][] = [
      [apiResponse({ ok: false, error: "ratelimited" }, 429, { "retry-after": "3" }), "retry", "slack_rate_limited"],
      [apiResponse({ ok: false, error: "internal_error" }), "uncertain", "slack_api_uncertain"],
      [apiResponse({ ok: false, error: "future_error" }), "uncertain", "slack_api_uncertain"],
      [apiResponse({ ok: false, error: "invalid_auth" }), "failed", "slack_authorization_rejected"],
      [apiResponse({ ok: false, error: "channel_not_found" }), "failed", "slack_configuration_rejected"],
      [apiResponse({ ok: false, error: "invalid_blocks" }), "failed", "slack_request_invalid"],
      [apiResponse({ ok: false }, 503), "uncertain", "slack_server_uncertain"],
      [new Error(`network failed with ${BOT_TOKEN}`), "uncertain", "slack_network_uncertain"],
      [apiResponse("not-json"), "uncertain", "slack_response_uncertain"],
      [apiResponse(null), "uncertain", "slack_response_uncertain"],
      [apiResponse({ ok: true, channel: "C123" }), "uncertain", "slack_response_uncertain"],
    ];

    for (const [step, outcome, errorCode] of cases) {
      const transport = scriptedFetch(step);
      const result = await sinkWith(transport.fetch).execute(action("post_parent"), "execute");
      expect(result).toMatchObject({ outcome, errorCode });
      expect(JSON.stringify(result)).not.toContain(BOT_TOKEN);
    }
  });

  it("honors Retry-After without persisting response content", async () => {
    const transport = scriptedFetch(
      apiResponse({ ok: false, error: "rate_limited", detail: BOT_TOKEN }, 429, {
        "retry-after": "2.25",
      }),
    );
    const result = await sinkWith(transport.fetch).execute(action("post_parent"), "execute");

    expect(result).toEqual({
      outcome: "retry",
      errorCode: "slack_rate_limited",
      retryAtMs: NOW_MS + 2_250,
    });
  });

  it("aborts a hung write before the outbox claim lease and marks it uncertain", async () => {
    const hangingFetch = (async (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), {
          once: true,
        });
      })) as typeof fetch;

    const result = await sinkWith(hangingFetch, NOW_MS, { requestTimeoutMs: 1 }).execute(
      action("post_parent"),
      "execute",
    );

    expect(result).toEqual({
      outcome: "uncertain",
      errorCode: "slack_network_uncertain",
    });
  });

  it("rejects invalid local config/action before external I/O and never throws", async () => {
    const transport = scriptedFetch();
    const invalidSink = new SlackSink({
      sinkId: "slack-primary",
      botToken: "",
      channelId: "not-a-channel",
      fetch: transport.fetch,
    });

    await expect(invalidSink.execute(action("post_parent"), "execute")).resolves.toEqual({
      outcome: "failed",
      errorCode: "slack_action_invalid",
    });
    await expect(
      sinkWith(transport.fetch).execute(
        action("update_parent", { deliveryRef: currentRef({ destinationId: "not-a-channel" }) }),
        "execute",
      ),
    ).resolves.toEqual({ outcome: "failed", errorCode: "slack_action_invalid" });
    await expect(
      sinkWith(transport.fetch).execute(
        action("update_parent", { deliveryRef: currentRef({ messageId: "not-a-ts" }) }),
        "execute",
      ),
    ).resolves.toEqual({ outcome: "failed", errorCode: "slack_action_invalid" });
    await expect(
      sinkWith(transport.fetch).execute(
        action("post_parent", { payload: { envelope: {} } }),
        "execute",
      ),
    ).resolves.toEqual({ outcome: "failed", errorCode: "slack_payload_invalid" });
    expect(transport.calls).toHaveLength(0);
  });

  it("does not accept the parent timestamp as a reply effect", async () => {
    const target = action("post_recovery");
    const transport = scriptedFetch(
      apiResponse({
        ok: true,
        channel: "C123",
        ts: target.deliveryRef!.conversationId,
      }),
    );

    await expect(sinkWith(transport.fetch).execute(target, "execute")).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      errorCode: "slack_response_reference_mismatch",
    });
  });

  it("keeps an existing incident on its persisted channel after the default changes", async () => {
    const oldChannelRef = currentRef({ destinationId: "C999" });
    const target = action("update_parent", { deliveryRef: oldChannelRef });
    const transport = scriptedFetch(
      apiResponse({ ok: true, channel: "C999", ts: oldChannelRef.messageId }),
    );

    const result = await sinkWith(transport.fetch).execute(target, "execute");

    expectSuccess(result);
    const body = JSON.parse(String(transport.calls[0].init?.body)) as Record<string, unknown>;
    expect(body.channel).toBe("C999");
    expect(result.receipt.deliveryRef).toEqual(oldChannelRef);
  });
});

describe("SlackSink reconciliation", () => {
  it("recovers a unique parent by channel, time window, and exact metadata", async () => {
    const target = action("post_parent");
    const effectTs = slackTs(START_MS + 500);
    const transport = scriptedFetch(
      apiResponse({
        ok: true,
        messages: [{ ts: effectTs, metadata: metadataFor(target) }],
        has_more: false,
        response_metadata: { next_cursor: "" },
      }),
    );

    const result = await sinkWith(transport.fetch).execute(target, "reconcile");

    expectSuccess(result);
    expect(result.receipt.deliveryRef.messageId).toBe(effectTs);
    const url = new URL(transport.calls[0].url);
    expect(url.pathname).toBe("/api/conversations.history");
    expect(url.searchParams.get("channel")).toBe("C123");
    expect(url.searchParams.get("include_all_metadata")).toBe("true");
    expect(url.searchParams.get("oldest")).toBe(slackTs(START_MS - 1_000).slice(0, -3));
  });

  it("waits through visibility grace, then retries only after a complete zero-match query", async () => {
    const empty = () =>
      apiResponse({
        ok: true,
        messages: [],
        has_more: false,
        response_metadata: { next_cursor: "" },
      });

    const withinGrace = await sinkWith(scriptedFetch(empty()).fetch, START_MS + 2_000).execute(
      action("post_parent"),
      "reconcile",
    );
    expect(withinGrace).toEqual({
      outcome: "uncertain",
      errorCode: "slack_reconcile_visibility_pending",
      reconcileAtMs: START_MS + 5_000,
    });

    const afterGrace = await sinkWith(scriptedFetch(empty()).fetch, START_MS + 6_000).execute(
      action("post_parent"),
      "reconcile",
    );
    expect(afterGrace).toEqual({ outcome: "retry", errorCode: "slack_effect_absent" });
  });

  it.each([
    [
      "multiple exact matches",
      (target: NotificationAction) => [
        { ts: slackTs(START_MS + 100), metadata: metadataFor(target) },
        { ts: slackTs(START_MS + 200), metadata: metadataFor(target) },
      ],
      "slack_reconcile_multiple_matches",
    ],
    [
      "a digest conflict",
      (target: NotificationAction) => [
        { ts: slackTs(START_MS + 100), metadata: metadataFor(target, `sha256:${"e".repeat(64)}`) },
      ],
      "slack_reconcile_metadata_conflict",
    ],
    [
      "missing required metadata on the same action",
      (target: NotificationAction) => [
        {
          ts: slackTs(START_MS + 100),
          metadata: {
            event_type: "alert_control_plane_action",
            event_payload: {
              action_id: target.actionId,
              incident_id: target.incidentId,
              generation: target.generation,
            },
          },
        },
      ],
      "slack_reconcile_metadata_conflict",
    ],
  ] as const)("requires manual handling for %s", async (_label, messages, errorCode) => {
    const target = action("post_parent");
    const transport = scriptedFetch(
      apiResponse({ ok: true, messages: messages(target), has_more: false }),
    );

    await expect(sinkWith(transport.fetch).execute(target, "reconcile")).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      errorCode,
    });
  });

  it("ignores matching metadata outside the attempt time window", async () => {
    const target = action("post_parent");
    const transport = scriptedFetch(
      apiResponse({
        ok: true,
        messages: [{ ts: slackTs(START_MS - 5_000), metadata: metadataFor(target) }],
        has_more: false,
      }),
    );

    await expect(sinkWith(transport.fetch).execute(target, "reconcile")).resolves.toEqual({
      outcome: "retry",
      errorCode: "slack_effect_absent",
    });
  });

  it("follows every cursor page before deciding", async () => {
    const target = action("post_parent");
    const effectTs = slackTs(START_MS + 250);
    const transport = scriptedFetch(
      apiResponse({
        ok: true,
        messages: [{ ts: slackTs(START_MS + 100) }],
        has_more: true,
        response_metadata: { next_cursor: "cursor-2" },
      }),
      apiResponse({
        ok: true,
        messages: [{ ts: effectTs, metadata: metadataFor(target) }],
        has_more: false,
        response_metadata: { next_cursor: "" },
      }),
    );

    const result = await sinkWith(transport.fetch).execute(target, "reconcile");

    expectSuccess(result);
    expect(transport.calls).toHaveLength(2);
    expect(new URL(transport.calls[1].url).searchParams.get("cursor")).toBe("cursor-2");
  });

  it("sends incomplete pagination to manual reconciliation", async () => {
    const transport = scriptedFetch(
      apiResponse({ ok: true, messages: [], has_more: true, response_metadata: {} }),
    );

    await expect(
      sinkWith(transport.fetch).execute(action("post_parent"), "reconcile"),
    ).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      errorCode: "slack_reconcile_incomplete",
    });
  });

  it("sends deterministic reconciliation rejection to manual handling", async () => {
    await expect(
      sinkWith(scriptedFetch(apiResponse({ ok: false, error: "invalid_auth" })).fetch).execute(
        action("post_parent"),
        "reconcile",
      ),
    ).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      errorCode: "slack_reconcile_incomplete",
    });
  });

  it("keeps ambiguous query failures uncertain", async () => {
    for (const step of [
      apiResponse({ ok: false }, 503),
      new Error(`query failed with ${BOT_TOKEN}`),
      apiResponse("not-json"),
    ]) {
      const result = await sinkWith(scriptedFetch(step).fetch).execute(
        action("post_parent"),
        "reconcile",
      );
      expect(result).toEqual({
        outcome: "uncertain",
        errorCode: "slack_reconcile_uncertain",
        reconcileAtMs: undefined,
      });
      expect(JSON.stringify(result)).not.toContain(BOT_TOKEN);
    }
  });

  it.each(["update_parent", "post_recovery", "post_analysis"] as const)(
    "reconciles a unique %s effect at the existing delivery reference",
    async (type) => {
      const target = action(type);
      const root = target.deliveryRef!.messageId;
      const effectTs = type === "update_parent" ? root : slackTs(START_MS + 500);
      const transport = scriptedFetch(
        apiResponse({
          ok: true,
          messages: [
            {
              ts: effectTs,
              ...(type === "update_parent" ? {} : { thread_ts: root }),
              metadata: metadataFor(target),
            },
          ],
          has_more: false,
        }),
      );

      const result = await sinkWith(transport.fetch).execute(target, "reconcile");

      expectSuccess(result);
      expect(result.receipt.deliveryRef).toEqual(target.deliveryRef);
      expect(result.receipt.externalEffectId).toBe(effectTs);
      expect(transport.calls[0].url).toContain(
        type === "update_parent" ? "conversations.history" : "conversations.replies",
      );
    },
  );

  it("lets an uncertain outbox claim reconcile without replaying the write", async () => {
    const store = new InMemoryIncidentStore();
    store.putGeneration(incidentGeneration({ deliveryRef: null, state: "opening" }));
    const pending = pendingAction({
      type: "post_parent",
      payload: action("post_parent").payload as Record<string, unknown>,
      status: "uncertain",
      startedAtMs: START_MS,
      nextRunAtMs: null,
      reconcileAtMs: NOW_MS,
      finalDeadlineAtMs: NOW_MS + 60_000,
    });
    store.putAction(pending);
    const projected = await projectNotificationAction(pending, null, "slack-primary");
    const effectTs = slackTs(START_MS + 500);
    const transport = scriptedFetch(
      apiResponse({
        ok: true,
        messages: [{ ts: effectTs, metadata: metadataFor(projected) }],
        has_more: false,
      }),
    );
    const sink = sinkWith(transport.fetch);
    const runner = new OutboxRunner(
      store,
      new NotificationActionExecutor(store, sink),
    );

    await expect(runner.runOne(NOW_MS)).resolves.toMatchObject({
      mode: "reconcile",
      outcome: "success",
    });
    expect(store.getAction(pending.actionId)?.status).toBe("completed");
    expect(transport.calls).toHaveLength(1);
    expect(transport.calls[0].init?.method).toBe("GET");
  });
});

describe("SlackSink import boundary", () => {
  it("does not import incident, store, outbox, or the Worker entrypoint", () => {
    expect(slackSource).toContain("export class SlackSink");
    expect(slackSource).not.toMatch(
      /from\s+["']\.\/(?:store|incident|outbox|index)["']/,
    );
  });

  it("keeps the incident authority free of Slack symbols", () => {
    expect(incidentSource).not.toMatch(/from\s+["']\.\/slack["']/);
    expect(incidentSource).not.toMatch(/\bSlack(?:Sink|Message|Block|Thread|Channel)\b/);
  });
});
