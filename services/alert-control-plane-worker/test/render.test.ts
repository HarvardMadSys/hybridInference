import { describe, expect, it } from "vitest";

import {
  escapeSlackMrkdwn,
  renderParent,
  renderRecoveryReply,
  type IncidentRenderState,
} from "../src/render";
import type { CanonicalAlertEnvelope, SlackMessage } from "../src/types";
import { createCanonicalEnvelope } from "../src/validation";

const TEST_NOW = Date.parse("2026-07-20T00:00:00Z");

function trusted(): Record<string, unknown> {
  return {
    environment: "staging",
    source: "gateway",
    principal: "staging-gateway",
    deployment_id: "gateway-20260719-1",
    deployment_sha: "a".repeat(40),
    artifact_digest: `sha256:${"b".repeat(64)}`,
    registry_version: 7,
  };
}

function event(status: "firing" | "resolved" = "firing"): Record<string, unknown> {
  return {
    schema_version: 1,
    event_id: status === "firing" ? "event-firing-1" : "event-resolved-1",
    alert_type: "provider_circuit_open",
    fingerprint: "provider-circuit:openai",
    status,
    severity: status === "firing" ? "error" : "info",
    title: status === "firing" ? "Provider circuit opened" : "Provider circuit recovered",
    occurred_at: status === "firing" ? "2026-07-19T06:00:00Z" : "2026-07-19T06:04:12Z",
    summary:
      status === "firing"
        ? "The provider refused connections"
        : "The provider accepted a successful probe",
    context:
      status === "firing"
        ? {
            provider: "openai",
            availability: 0,
            error: "Connection refused",
            affected_users: 55,
            consecutive_failures: 8,
            reason: "connection_refused",
          }
        : {
            provider: "openai",
            availability: 1,
            final_failure_count: 8,
            outage_duration_ms: 252_000,
          },
    evidence_refs: ["config/models.yaml"],
  };
}

function envelope(status: "firing" | "resolved" = "firing"): CanonicalAlertEnvelope {
  return createCanonicalEnvelope(event(status), trusted(), { now: TEST_NOW });
}

function renderState(actionId = "action-parent-1"): IncidentRenderState {
  return {
    action_id: actionId,
    incident_id: "incident-1",
    generation: 3,
    occurrence_count: 8,
    first_seen: "2026-07-19T06:00:00.000Z",
    last_seen: "2026-07-19T06:04:12.000Z",
  };
}

function renderedText(message: SlackMessage): string {
  const values = [message.text];
  for (const block of message.blocks) {
    if (block.type === "header") values.push(block.text.text);
    if (block.type === "section") {
      if (block.text) values.push(block.text.text);
      for (const item of block.fields ?? []) values.push(item.text);
    }
    if (block.type === "context") {
      for (const item of block.elements) values.push(item.text);
    }
  }
  return values.join("\n");
}

describe("Slack renderer", () => {
  it("renders environment, source, and deployment only from trusted metadata", () => {
    const message = renderParent(envelope(), renderState());
    const text = renderedText(message);

    expect(text).toContain("STAGING");
    expect(text).toContain("Source: gateway");
    expect(text).toContain("gateway-20260719-1@aaaaaaaaaaaa");
    expect(text).toContain("Provider");
    expect(text).toContain("openai");
    expect(text).toContain("Availability");
    expect(text).toContain("0.0%");
  });

  it("escapes every producer-controlled mrkdwn value and blocks mention injection", () => {
    const unsafeEvent = {
      ...event(),
      fingerprint: "provider:<@U012345>",
      title: "Provider <@U012345> & degraded",
      summary: "Do not notify <!channel>; show <https://example.invalid|literal>",
      context: {
        provider: "<@U099999>",
        error: "upstream returned <!everyone> & <tag>",
      },
    };
    const unsafeEnvelope = createCanonicalEnvelope(unsafeEvent, trusted(), { now: TEST_NOW });
    const text = renderedText(renderParent(unsafeEnvelope, renderState()));

    expect(text).not.toContain("<@U");
    expect(text).not.toContain("<!channel>");
    expect(text).not.toContain("<!everyone>");
    expect(text).toContain("&lt;@U012345&gt;");
    expect(text).toContain("&lt;!channel&gt;");
    expect(text).toContain("&amp;");
  });

  it("attaches stable action metadata for downstream reconciliation", () => {
    const message = renderParent(envelope(), renderState("action-parent-stable"));
    expect(message.metadata).toEqual({
      event_type: "alert_control_plane_action",
      event_payload: {
        action_id: "action-parent-stable",
        incident_id: "incident-1",
        generation: 3,
      },
    });
  });

  it("renders a resolved parent and an ordered recovery reply", () => {
    const resolved = envelope("resolved");
    const parent = renderParent(resolved, renderState("action-update-1"));
    const recovery = renderRecoveryReply(resolved, renderState("action-recovery-1"));

    expect(parent.text).toContain("RESOLVED");
    expect(renderedText(recovery)).toContain("Recovery confirmed");
    expect(renderedText(recovery)).toContain("Final failure count");
    expect(renderedText(recovery)).toContain("4m 12s");
    expect(recovery.metadata.event_payload.action_id).toBe("action-recovery-1");
    expect(() => renderRecoveryReply(envelope("firing"), renderState())).toThrow(/resolved event/);
  });

  it("stays within Slack text and block limits after entity expansion", () => {
    const large = createCanonicalEnvelope(
      {
        ...event(),
        title: "<&>".repeat(166),
        summary: "<&>".repeat(1_300),
        context: { provider: "<&>".repeat(80), error: "<&>".repeat(600) },
      },
      trusted(),
      { now: TEST_NOW },
    );
    const message = renderParent(large, renderState());

    expect([...message.text].length).toBeLessThanOrEqual(4_000);
    for (const block of message.blocks) {
      if (block.type === "header") expect([...block.text.text].length).toBeLessThanOrEqual(150);
      if (block.type === "section") {
        if (block.text) expect([...block.text.text].length).toBeLessThanOrEqual(3_000);
        for (const item of block.fields ?? []) {
          expect([...item.text].length).toBeLessThanOrEqual(2_000);
        }
      }
      if (block.type === "context") {
        for (const item of block.elements) {
          expect([...item.text].length).toBeLessThanOrEqual(3_000);
        }
      }
    }
  });

  it("escapes in entity-safe order", () => {
    expect(escapeSlackMrkdwn("<&>")).toBe("&lt;&amp;&gt;");
  });
});
