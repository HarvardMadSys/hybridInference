import { describe, expect, it } from "vitest";

import { parseAlertEvent, parseCompletion, ValidationError } from "../src/validation";

function validAlert(): Record<string, unknown> {
  return {
    version: "2",
    alert_id: "alert-1",
    fingerprint: "gateway:provider:openai",
    source: "hybrid-inference-gateway",
    status: "firing",
    severity: "error",
    title: "Provider failed",
    occurred_at: "2026-07-19T12:00:00Z",
    summary: "OpenAI returned errors",
    context: { provider: "openai", api_key: "must-not-leak" },
    deployment_sha: "a".repeat(40),
    evidence_refs: ["request-count:20"],
  };
}

describe("AlertEvent V2 validation", () => {
  it("accepts and sanitizes the producer-neutral contract", () => {
    const event = parseAlertEvent(validAlert());
    expect(event).toMatchObject({
      version: "2",
      alert_id: "alert-1",
      occurred_at: "2026-07-19T12:00:00.000Z",
      context: { provider: "openai", api_key: "[REDACTED]" },
    });
  });

  it.each(["environment", "slack_text"])("rejects producer-owned %s", (field) => {
    expect(() => parseAlertEvent({ ...validAlert(), [field]: "untrusted" })).toThrow(
      ValidationError,
    );
  });

  it("rejects unknown versions, oversized values, and malformed timestamps", () => {
    expect(() => parseAlertEvent({ ...validAlert(), version: "1" })).toThrow(/version/);
    expect(() => parseAlertEvent({ ...validAlert(), title: "x".repeat(501) })).toThrow(/title/);
    expect(() => parseAlertEvent({ ...validAlert(), occurred_at: "tomorrow" })).toThrow(
      /occurred_at/,
    );
    expect(() => parseAlertEvent({ ...validAlert(), deployment_sha: "abc123" })).toThrow(
      /deployment_sha/,
    );
  });

  it("bounds nested context and redacts bearer/key patterns", () => {
    const alert = validAlert();
    alert.summary = "failure token=abcdefghi";
    alert.context = {
      detail: "Authorization: Bearer abcdefghijklmnop",
      nested: {
        password_hint: "secret",
        remote_ip: "192.0.2.1",
        response: "api_key=abcdefghi",
      },
    };
    const parsed = parseAlertEvent(alert);
    expect(parsed.summary).toBe("failure token=[REDACTED]");
    expect(parsed.context).toEqual({
      detail: "Authorization: Bearer [REDACTED]",
      nested: {
        password_hint: "[REDACTED]",
        remote_ip: "[REDACTED]",
        response: "api_key=[REDACTED]",
      },
    });
    alert.context = { values: Array.from({ length: 51 }, (_, index) => index) };
    expect(() => parseAlertEvent(alert)).toThrow(/50 items/);

    alert.context = { a: { b: { c: { d: { e: "[TRUNCATED]" } } } } };
    expect(parseAlertEvent(alert).context).toEqual(alert.context);
    alert.context = { a: { b: { c: { d: { e: { too: "deep" } } } } } };
    expect(() => parseAlertEvent(alert)).toThrow(/maximum depth/);
  });

  it("keeps prototype-like context keys as inert data", () => {
    const alert = validAlert();
    alert.context = JSON.parse('{"__proto__":"literal","constructor":"literal"}') as object;
    const parsed = parseAlertEvent(alert);
    expect(Object.hasOwn(parsed.context, "__proto__")).toBe(true);
    expect(parsed.context.__proto__).toBe("literal");
    expect(Object.getPrototypeOf(parsed.context)).toBeNull();
  });
});

describe("completion validation", () => {
  const analysis = {
    summary: "Upstream throttling",
    classification: "upstream_provider",
    confidence: 0.8,
    impact: "Requests fail",
    evidence: ["HTTP 429"],
    likely_cause: "Provider quota",
    recommended_actions: ["Wait for recovery"],
    issue_recommendation: "none",
    draft_pr_recommendation: "none",
  };

  it("accepts strict success and failure payloads", () => {
    expect(parseCompletion({ status: "success", analysis })).toMatchObject({
      status: "success",
      analysis: { classification: "upstream_provider" },
    });
    expect(
      parseCompletion({
        status: "failure",
        error: "checkout failed",
        run_url: "https://github.com/org/repo/actions/runs/1",
      }),
    ).toMatchObject({ status: "failure" });
  });

  it("rejects incomplete, extra, and unsafe failure payloads", () => {
    expect(() => parseCompletion({ status: "success", analysis: { ...analysis, extra: true } })).toThrow(
      /unsupported/,
    );
    expect(() => parseCompletion({ status: "failure", error: "failed" })).toThrow(/run_url/);
    expect(() =>
      parseCompletion({ status: "failure", error: "failed", run_url: "http://example.com/run" }),
    ).toThrow(/HTTPS/);
  });
});
