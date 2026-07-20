import { describe, expect, it } from "vitest";

import worker from "../src/index";
import type { Env } from "../src/types";

const env = {
  ALERT_RELAY_V2_STAGING_TOKEN: "staging-secret",
  ALERT_RELAY_V2_WORKFLOW_TOKEN: "workflow-secret",
} as Env;

const readyEnv = {
  SLACK_BOT_TOKEN: "xoxb-secret",
  SLACK_CHANNEL_ID: "C123",
  GITHUB_TOKEN: "github-secret",
  GITHUB_REPOSITORY: "org/repo",
  GITHUB_API_BASE_URL: "https://api.github.test",
  CODEX_MODEL: "model",
  MODEL_BASE_URL: "https://model.test/v1",
  ALERT_RELAY_V2_WORKFLOW_TOKEN: "workflow-secret",
  ALERT_RELAY_V2_STAGING_TOKEN: "staging-secret",
  ALERT_RELAY_V2_PRODUCTION_TOKEN: "production-secret",
} as Env;

function alert(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    version: "2",
    alert_id: "alert-1",
    fingerprint: "gateway:test",
    source: "gateway",
    status: "firing",
    severity: "error",
    title: "Failure",
    occurred_at: "2026-07-19T12:00:00Z",
    summary: "Failure summary",
    context: {},
    ...extra,
  };
}

describe("Worker HTTP contract", () => {
  it("authenticates alerts before parsing producer input", async () => {
    const response = await worker.fetch(
      new Request("https://relay.test/v2/alerts", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ environment: "production" }),
      }),
      env,
    );
    expect(response.status).toBe(401);
  });

  it("rejects environment and slack_text even with a valid producer credential", async () => {
    for (const extra of [{ environment: "production" }, { slack_text: "<!channel>" }]) {
      const response = await worker.fetch(
        new Request("https://relay.test/v2/alerts", {
          method: "POST",
          headers: {
            authorization: "Bearer staging-secret",
            "content-type": "application/json",
          },
          body: JSON.stringify(alert(extra)),
        }),
        env,
      );
      expect(response.status).toBe(400);
      expect(await response.json()).toMatchObject({ error: expect.stringContaining("unsupported") });
    }
  });

  it("keeps job APIs isolated behind the workflow credential", async () => {
    const jobId = "11111111-1111-4111-8111-111111111111";
    for (const [method, path] of [
      ["GET", `/v2/jobs/${jobId}`],
      ["POST", `/v2/jobs/${jobId}/complete`],
    ]) {
      const response = await worker.fetch(
        new Request(`https://relay.test${path}`, {
          method,
          headers: {
            authorization: "Bearer staging-secret",
            "content-type": "application/json",
          },
          body: method === "POST" ? JSON.stringify({ status: "failure" }) : undefined,
        }),
        env,
      );
      expect(response.status).toBe(401);
    }
  });

  it("reports V2 as opt-in and unconfigured without bindings", async () => {
    const response = await worker.fetch(
      new Request("https://relay.test/healthz"),
      {} as Env,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      status: "unconfigured",
      ready: false,
      version: "2",
    });
  });

  it("is ready only with distinct producer tokens and credential-free HTTPS URLs", async () => {
    const healthy = await worker.fetch(new Request("https://relay.test/healthz"), readyEnv);
    expect(healthy.status).toBe(200);
    expect(await healthy.json()).toEqual({ status: "ok", ready: true, version: "2" });

    const duplicateTokens = await worker.fetch(
      new Request("https://relay.test/healthz"),
      {
        ...readyEnv,
        ALERT_RELAY_V2_PRODUCTION_TOKEN: "staging-secret",
      },
    );
    expect(duplicateTokens.status).toBe(503);
    expect(await duplicateTokens.json()).toEqual({
      status: "misconfigured",
      ready: false,
      version: "2",
    });

    const insecureModel = await worker.fetch(
      new Request("https://relay.test/healthz"),
      { ...readyEnv, MODEL_BASE_URL: "http://model.test/v1" },
    );
    expect(insecureModel.status).toBe(503);
    expect(await insecureModel.json()).toMatchObject({
      status: "misconfigured",
      ready: false,
    });

    const credentialedGitHub = await worker.fetch(
      new Request("https://relay.test/healthz"),
      { ...readyEnv, GITHUB_API_BASE_URL: "https://user:pass@api.github.test" },
    );
    expect(credentialedGitHub.status).toBe(503);
    expect(await credentialedGitHub.json()).toMatchObject({
      status: "misconfigured",
      ready: false,
    });
  });
});
