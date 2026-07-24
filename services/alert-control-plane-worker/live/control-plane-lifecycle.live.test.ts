import { describe, expect, it } from "vitest";

const CONFIRMATION = "run-staging-control-plane-lifecycle";
const DEADLINE_MS = 15 * 60 * 1_000;
const POLL_INTERVAL_MS = 10_000;
const CHANNEL_ID_RE = /^[CGD][A-Z0-9]{1,255}$/;

interface RuntimeProcess {
  readonly env: Readonly<Record<string, string | undefined>>;
}

interface LiveConfig {
  readonly controlPlaneUrl: string;
  readonly producerToken: string;
  readonly slackBotToken: string;
  readonly channelId: string;
  readonly runId: string;
}

interface IncidentAcknowledgement {
  readonly accepted: true;
  readonly incident_id: string;
  readonly generation: number;
  readonly occurrence_count: number;
}

interface SlackMessage {
  readonly ts?: string;
  readonly thread_ts?: string;
  readonly blocks?: readonly {
    readonly fields?: readonly { readonly text?: string }[];
  }[];
  readonly metadata?: {
    readonly event_type?: string;
    readonly event_payload?: {
      readonly action_id?: string;
      readonly incident_id?: string;
      readonly generation?: number;
      readonly payload_digest?: string;
    };
  };
}

interface SlackPage {
  readonly ok?: boolean;
  readonly error?: string;
  readonly messages?: readonly SlackMessage[];
  readonly response_metadata?: { readonly next_cursor?: string };
}

function environment(): RuntimeProcess["env"] {
  const runtime = (
    globalThis as typeof globalThis & { process?: RuntimeProcess }
  ).process;
  if (runtime === undefined) {
    throw new Error("live lifecycle requires a Node-compatible process");
  }
  return runtime.env;
}

function required(name: string): string {
  const value = environment()[name]?.trim();
  if (value === undefined || value.length === 0) {
    throw new Error(`live lifecycle requires ${name}`);
  }
  return value;
}

function config(): LiveConfig {
  if (required("CONTROL_PLANE_LIVE_CONFIRM") !== CONFIRMATION) {
    throw new Error(
      `live lifecycle requires CONTROL_PLANE_LIVE_CONFIRM=${CONFIRMATION}`,
    );
  }
  const controlPlaneUrl = required("CONTROL_PLANE_URL").replace(/\/+$/u, "");
  const channelId = required("SLACK_CHANNEL_ID");
  if (!controlPlaneUrl.startsWith("https://")) {
    throw new Error("CONTROL_PLANE_URL must use HTTPS");
  }
  if (!CHANNEL_ID_RE.test(channelId)) {
    throw new Error("SLACK_CHANNEL_ID must be a Slack conversation ID");
  }
  return {
    controlPlaneUrl,
    producerToken: required("CONTROL_PLANE_PRODUCER_TOKEN"),
    slackBotToken: required("SLACK_BOT_TOKEN"),
    channelId,
    runId: required("CONTROL_PLANE_SYNTHETIC_RUN_ID").replace(
      /[^A-Za-z0-9._-]/gu,
      "-",
    ),
  };
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function event(
  runId: string,
  sequence: number,
  status: "firing" | "resolved",
): Record<string, unknown> {
  return {
    schema_version: 1,
    event_id: `c2-${runId}-${sequence}`,
    alert_type: "provider_circuit_open",
    fingerprint: `c2-synthetic:${runId}`,
    status,
    severity: "warn",
    title: `[C2 SYNTHETIC] Staging lifecycle ${runId}`,
    occurred_at: new Date(Date.now() + sequence).toISOString(),
    summary:
      status === "firing"
        ? "Synthetic staging event used to verify the authenticated control plane."
        : "Synthetic staging recovery used to verify the authenticated control plane.",
    context: {
      provider: `c2-synthetic:${runId}`,
      availability: status === "firing" ? 0 : 1,
      reason:
        status === "firing" ? "availability_below_threshold" : "unknown",
      ...(status === "firing"
        ? { consecutive_failures: sequence }
        : { final_failure_count: 2, outage_duration_ms: 1_000 }),
    },
    evidence_refs: ["live/control-plane-lifecycle.live.test.ts"],
  };
}

async function postEvent(
  live: LiveConfig,
  sequence: number,
  status: "firing" | "resolved",
): Promise<IncidentAcknowledgement> {
  const response = await fetch(`${live.controlPlaneUrl}/v1/events`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${live.producerToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(event(live.runId, sequence, status)),
  });
  if (!response.ok) {
    throw new Error(`control_plane_event_failed_${response.status}`);
  }
  const value = (await response.json()) as Partial<IncidentAcknowledgement>;
  if (
    value.accepted !== true ||
    typeof value.incident_id !== "string" ||
    !Number.isSafeInteger(value.generation) ||
    !Number.isSafeInteger(value.occurrence_count)
  ) {
    throw new Error("control_plane_ack_invalid");
  }
  return value as IncidentAcknowledgement;
}

async function slackPage(
  live: LiveConfig,
  method: "conversations.history" | "conversations.replies",
  parameters: URLSearchParams,
): Promise<SlackPage> {
  for (;;) {
    const response = await fetch(
      `https://slack.com/api/${method}?${parameters.toString()}`,
      {
        headers: { authorization: `Bearer ${live.slackBotToken}` },
      },
    );
    if (response.status === 429) {
      const retryAfter = Number(response.headers.get("retry-after"));
      await sleep(
        Number.isFinite(retryAfter) && retryAfter > 0
          ? retryAfter * 1_000
          : 60_000,
      );
      continue;
    }
    let page: SlackPage;
    try {
      page = (await response.json()) as SlackPage;
    } catch {
      throw new Error("slack_readback_invalid_json");
    }
    if (!response.ok || page.ok !== true) {
      const code =
        typeof page.error === "string" &&
        /^[a-z][a-z0-9_]{0,63}$/u.test(page.error)
          ? page.error
          : "unclassified";
      throw new Error(`slack_readback_failed_${code}`);
    }
    return page;
  }
}

async function history(
  live: LiveConfig,
  oldest: string,
): Promise<readonly SlackMessage[]> {
  const messages: SlackMessage[] = [];
  let cursor = "";
  const seen = new Set<string>();
  for (;;) {
    const parameters = new URLSearchParams({
      channel: live.channelId,
      include_all_metadata: "true",
      inclusive: "true",
      limit: "100",
      oldest,
    });
    if (cursor.length > 0) parameters.set("cursor", cursor);
    const page = await slackPage(live, "conversations.history", parameters);
    messages.push(...(page.messages ?? []));
    const next = page.response_metadata?.next_cursor?.trim() ?? "";
    if (next.length === 0) return messages;
    if (seen.has(next)) throw new Error("slack_history_cursor_loop");
    seen.add(next);
    cursor = next;
  }
}

function belongsTo(
  message: SlackMessage,
  incidentId: string,
  generation: number,
): boolean {
  return (
    message.metadata?.event_type === "alert_control_plane_action" &&
    typeof message.metadata.event_payload?.action_id === "string" &&
    message.metadata.event_payload.action_id.length > 0 &&
    message.metadata.event_payload?.incident_id === incidentId &&
    message.metadata.event_payload.generation === generation &&
    typeof message.metadata.event_payload.payload_digest === "string" &&
    /^sha256:[a-f0-9]{64}$/u.test(
      message.metadata.event_payload.payload_digest,
    )
  );
}

function occurrenceCount(message: SlackMessage): number | null {
  for (const block of message.blocks ?? []) {
    for (const field of block.fields ?? []) {
      const match = /^\*Occurrences\*\n([1-9][0-9]*)$/u.exec(
        field.text ?? "",
      );
      if (match?.[1] !== undefined) return Number(match[1]);
    }
  }
  return null;
}

async function waitForParent(
  live: LiveConfig,
  oldest: string,
  incidentId: string,
  generation: number,
  occurrences: number,
): Promise<SlackMessage & { readonly ts: string }> {
  const deadline = Date.now() + DEADLINE_MS;
  for (;;) {
    const matches = (await history(live, oldest)).filter(
      (message) =>
        belongsTo(message, incidentId, generation) &&
        occurrenceCount(message) === occurrences,
    );
    if (matches.length === 1 && matches[0]?.ts !== undefined) {
      return matches[0] as SlackMessage & { readonly ts: string };
    }
    if (matches.length > 1) {
      throw new Error(`duplicate_parent_generation_${generation}`);
    }
    if (Date.now() >= deadline) {
      throw new Error(`parent_generation_${generation}_not_visible`);
    }
    await sleep(POLL_INTERVAL_MS);
  }
}

async function replies(
  live: LiveConfig,
  parentTs: string,
): Promise<readonly SlackMessage[]> {
  const page = await slackPage(
    live,
    "conversations.replies",
    new URLSearchParams({
      channel: live.channelId,
      ts: parentTs,
      include_all_metadata: "true",
      limit: "100",
    }),
  );
  return page.messages ?? [];
}

async function waitForRecovery(
  live: LiveConfig,
  parentTs: string,
  incidentId: string,
  generation: number,
): Promise<SlackMessage & { readonly ts: string }> {
  const deadline = Date.now() + DEADLINE_MS;
  for (;;) {
    const matches = (await replies(live, parentTs)).filter(
      (message) =>
        message.ts !== parentTs &&
        belongsTo(message, incidentId, generation),
    );
    if (matches.length === 1 && matches[0]?.ts !== undefined) {
      return matches[0] as SlackMessage & { readonly ts: string };
    }
    if (matches.length > 1) throw new Error("duplicate_recovery_reply");
    if (Date.now() >= deadline) throw new Error("recovery_reply_not_visible");
    await sleep(POLL_INTERVAL_MS);
  }
}

describe.sequential("authenticated staging control-plane lifecycle", () => {
  it("runs firing, repeat, resolved, and re-fire with one parent per generation", async () => {
    const live = config();
    const oldest = String(Math.floor(Date.now() / 1_000) - 60);

    const firing = await postEvent(live, 1, "firing");
    expect(firing.generation).toBe(1);
    expect(firing.occurrence_count).toBe(1);
    const firstParent = await waitForParent(
      live,
      oldest,
      firing.incident_id,
      1,
      1,
    );

    const repeated = await postEvent(live, 2, "firing");
    expect(repeated.incident_id).toBe(firing.incident_id);
    expect(repeated.generation).toBe(1);
    expect(repeated.occurrence_count).toBe(2);
    const updatedParent = await waitForParent(
      live,
      oldest,
      firing.incident_id,
      1,
      2,
    );
    expect(updatedParent.ts).toBe(firstParent.ts);

    const resolved = await postEvent(live, 3, "resolved");
    expect(resolved.incident_id).toBe(firing.incident_id);
    expect(resolved.generation).toBe(1);
    const recovery = await waitForRecovery(
      live,
      firstParent.ts,
      firing.incident_id,
      1,
    );

    const refired = await postEvent(live, 4, "firing");
    expect(refired.incident_id).not.toBe(firing.incident_id);
    expect(refired.generation).toBe(2);
    const secondParent = await waitForParent(
      live,
      oldest,
      refired.incident_id,
      2,
      1,
    );
    expect(secondParent.ts).not.toBe(firstParent.ts);

    const finalParents = (await history(live, oldest)).filter(
      (message) =>
        belongsTo(message, firing.incident_id, 1) ||
        belongsTo(message, refired.incident_id, 2),
    );
    expect(
      finalParents.filter((message) =>
        belongsTo(message, firing.incident_id, 1),
      ),
    ).toHaveLength(1);
    expect(
      finalParents.filter((message) =>
        belongsTo(message, refired.incident_id, 2),
      ),
    ).toHaveLength(1);

    console.log(
      JSON.stringify({
        status: "passed",
        run_id: live.runId,
        channel_id: live.channelId,
        incident_id: firing.incident_id,
        generation_2_incident_id: refired.incident_id,
        generation_1_parent: firstParent.ts,
        generation_1_recovery: recovery.ts,
        generation_2_parent: secondParent.ts,
      }),
    );
  });
});
