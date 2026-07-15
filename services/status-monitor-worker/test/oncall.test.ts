import { afterEach, describe, expect, it, vi } from "vitest";

import type { Env } from "../src/env";
import {
  codexRelayConfig,
  type CodexAlertEvent,
  hasAlertDestination,
  modelAlertFingerprint,
  postCodexAlert,
  stormAlertFingerprint,
} from "../src/oncall";

const event: CodexAlertEvent = {
  version: "1",
  alert_id: "00000000-0000-4000-8000-000000000000",
  fingerprint: "status-monitor:model:a",
  source: "status-monitor-worker",
  status: "firing",
  severity: "error",
  title: "Model down: a",
  environment: "staging",
  occurred_at: "2026-06-25T00:00:00Z",
  summary: "a failed",
  context: { model_id: "a" },
  slack_text: "model a is down",
};

describe("alert fingerprints", () => {
  it("uses the same bounded model key for firing and recovery", () => {
    expect(modelAlertFingerprint("a")).toBe("status-monitor:model:a");

    const longFingerprint = modelAlertFingerprint("x".repeat(600));
    expect(longFingerprint).toHaveLength(512);
    expect(longFingerprint).toBe(modelAlertFingerprint("x".repeat(600)));
  });

  it("hashes storm model ids independently of input order", () => {
    const fingerprint = stormAlertFingerprint(["b", "a", "c"]);
    expect(fingerprint).toMatch(/^status-monitor:storm:[0-9a-f]{8}$/);
    expect(fingerprint).toBe(stormAlertFingerprint(["c", "b", "a"]));
  });
});

describe("relay configuration", () => {
  it("requires both relay bindings and trims their values", () => {
    const env = {
      CODEX_ONCALL_RELAY_URL: " https://relay.test/ ",
      CODEX_ONCALL_RELAY_TOKEN: " token ",
    } as Env;
    expect(codexRelayConfig(env)).toEqual({ baseUrl: "https://relay.test/", token: "token" });
    expect(codexRelayConfig({ CODEX_ONCALL_RELAY_URL: "https://relay.test" } as Env)).toBeNull();
  });

  it("recognizes relay, Slack, and no-destination configurations", () => {
    expect(
      hasAlertDestination({
        CODEX_ONCALL_RELAY_URL: "https://relay.test",
        CODEX_ONCALL_RELAY_TOKEN: "token",
      } as Env),
    ).toBe(true);
    expect(hasAlertDestination({ SLACK_WEBHOOK_URL: "https://hook.test" } as Env)).toBe(true);
    expect(hasAlertDestination({ SLACK_WEBHOOK_URL: "  " } as Env)).toBe(false);
    expect(hasAlertDestination({} as Env)).toBe(false);
  });
});

describe("postCodexAlert", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("posts the authenticated JSON event to /v1/alerts with a 10s timeout", async () => {
    const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => {
      return new Response(null, { status: 202 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const timeoutSpy = vi.spyOn(AbortSignal, "timeout");

    expect(
      await postCodexAlert({ baseUrl: "https://relay.test/base/", token: "secret" }, event),
    ).toBe(true);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://relay.test/base/v1/alerts");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({
      Authorization: "Bearer secret",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(init.body))).toEqual(event);
    expect(init.redirect).toBe("error");
    expect(timeoutSpy).toHaveBeenCalledWith(10_000);
  });
});
