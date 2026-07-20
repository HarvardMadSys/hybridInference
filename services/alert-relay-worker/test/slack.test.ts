import { afterEach, describe, expect, it, vi } from "vitest";

import {
  renderAnalysisReply,
  renderParent,
  renderRecoveryReply,
  renderUnavailableReply,
} from "../src/render";
import { SlackApiClient } from "../src/slack";
import type { Incident, OnCallAnalysis } from "../src/types";

function incident(overrides: Partial<Incident> = {}): Incident {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    environment: "staging",
    fingerprint: "gateway:provider:<@U1>",
    status: "firing",
    alert: {
      version: "2",
      alert_id: "alert-1",
      fingerprint: "gateway:provider:<@U1>",
      source: "gateway",
      status: "firing",
      severity: "error",
      title: "Provider <!channel> failed",
      occurred_at: "2026-07-19T12:00:00.000Z",
      summary: "Error from <@U1> & upstream",
      context: {
        provider: "openai<@U2>",
        probe: { model_id: "model-a", error: "upstream <!channel>" },
      },
      deployment_sha: "a".repeat(40),
    },
    resolutionAlert: null,
    occurrenceCount: 1,
    firstSeen: "2026-07-19T12:00:00.000Z",
    lastSeen: "2026-07-19T12:00:00.000Z",
    slackChannelId: "C123",
    slackThreadTs: "171.1",
    codexStatus: "investigating",
    analysisRef: null,
    parentDirty: false,
    parentVersion: 1,
    recoveryPending: false,
    recoveryMessageId: null,
    ...overrides,
  };
}

describe("unified Slack renderer", () => {
  it("shows pending until the deployment SHA is validated and stays mention-safe", () => {
    const message = renderParent(incident({ occurrenceCount: 4 }));
    const encoded = JSON.stringify(message);
    expect(message.text).toContain("[STAGING] FIRING");
    expect(encoded).toContain("dev@pending");
    expect(encoded).not.toContain(`dev@${"a".repeat(40)}`);
    expect(encoded).toContain("Failure count");
    expect(encoded).toContain("4");
    expect(encoded).toContain("Investigating");
    expect(encoded).toContain("Provider");
    expect(encoded).toContain("Probe · Error");
    expect(encoded).toContain("model-a");
    expect(encoded).not.toContain("<!channel>");
    expect(encoded).not.toContain("<@U1>");
    expect(encoded).not.toContain("<@U2>");
    expect(encoded).toContain("&lt;!channel&gt;");
    for (const block of message.blocks) {
      if ("text" in block && block.text) expect(block.text.text.length).toBeLessThanOrEqual(3_000);
      if ("fields" in block && block.fields) {
        expect(block.fields.length).toBeLessThanOrEqual(10);
        expect(block.fields.every((item) => item.text.length <= 2_000)).toBe(true);
      }
    }
  });

  it("renders validated and fallback analysis refs without trusting producer SHA", () => {
    expect(
      JSON.stringify(renderParent(incident({ analysisRef: "b".repeat(40) }))),
    ).toContain(`dev@${"b".repeat(40)}`);
    const fallback = JSON.stringify(renderParent(incident({ analysisRef: "dev" })));
    expect(fallback).toContain("dev@unavailable (analysis ref: dev)");
    expect(fallback).not.toContain(`dev@${"a".repeat(40)}`);
  });

  it("renders resolved parent and recovery duration/count in the same lifecycle", () => {
    const resolved = incident({
      status: "resolved",
      codexStatus: "resolved",
      occurrenceCount: 4,
      lastSeen: "2026-07-19T12:10:05.000Z",
      resolutionAlert: {
        ...incident().alert,
        alert_id: "alert-2",
        status: "resolved",
        severity: "info",
        title: "Provider recovered",
        summary: "Requests work again",
        context: { final_failure_count: 57 },
      },
    });
    expect(renderParent(resolved).text).toContain("RESOLVED");
    expect(JSON.stringify(renderParent(resolved))).toContain("Resolved");
    const recovery = JSON.stringify(renderRecoveryReply(resolved));
    expect(recovery).toContain("10m 5s");
    expect(recovery).toContain("Final failure count");
    expect(recovery).toContain("57");
  });

  it("renders analysis_ready distinctly from incident resolution", () => {
    const encoded = JSON.stringify(renderParent(incident({ codexStatus: "analysis_ready" })));
    expect(encoded).toContain("Analysis ready");
    expect(encoded).not.toContain("*Codex*\\nResolved");
  });

  it("renders bounded analysis and explicit unavailable replies with run URLs", () => {
    const analysis: OnCallAnalysis = {
      summary: "Upstream throttling",
      classification: "upstream_provider",
      confidence: 0.7,
      impact: "Some calls fail",
      evidence: ["Provider returned <@U1>"],
      likely_cause: "Quota",
      recommended_actions: ["Wait"],
      issue_recommendation: "none",
      draft_pr_recommendation: "none",
    };
    const analysisMessage = JSON.stringify(renderAnalysisReply(incident(), analysis, "thread-1"));
    expect(analysisMessage).toContain("Codex analysis");
    expect(analysisMessage).toContain("&lt;@U1&gt;");

    const unavailable = renderUnavailableReply(
      incident(),
      "Workflow failed",
      "https://github.com/org/repo/actions/runs/1",
    );
    expect(JSON.stringify(unavailable)).toContain("https://github.com/org/repo/actions/runs/1");
  });
});

describe("Slack bot API calls", () => {
  afterEach(() => vi.restoreAllMocks());

  it("uses postMessage for parents/replies and chat.update for repeat parent changes", async () => {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        body: JSON.parse(String(init?.body)) as Record<string, unknown>,
      });
      return Response.json({ ok: true, ts: "171.1" });
    });
    const slack = new SlackApiClient("xoxb-secret", "C123", fetcher as typeof fetch);
    const message = renderParent(incident());

    await expect(slack.postParent(message, incident().id)).resolves.toBe("171.1");
    await slack.updateParent("171.1", renderParent(incident({ occurrenceCount: 2 })));
    await slack.postReply("171.1", renderRecoveryReply(incident()), "reply-id");

    expect(calls.map((call) => call.url)).toEqual([
      "https://slack.com/api/chat.postMessage",
      "https://slack.com/api/chat.update",
      "https://slack.com/api/chat.postMessage",
    ]);
    expect(calls[0].body).toMatchObject({ channel: "C123", client_msg_id: incident().id });
    expect(calls[1].body).toMatchObject({ channel: "C123", ts: "171.1" });
    expect(calls[2].body).toMatchObject({ thread_ts: "171.1", reply_broadcast: false });
  });
});
