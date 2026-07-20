import { afterEach, describe, expect, it, vi } from "vitest";

import type { Env } from "../src/env";
import {
  alertRelayV2Config,
  codexRelayConfig,
  type CodexAlertEvent,
  createAlertEventV2,
  hasAlertDestination,
  modelAlertFingerprint,
  postAlertEventV2,
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
    expect(
      hasAlertDestination({
        ALERT_RELAY_V2_URL: "https://relay-v2.test",
        ALERT_RELAY_V2_TOKEN: "token",
      } as Env),
    ).toBe(true);
    expect(hasAlertDestination({} as Env)).toBe(false);
  });

  it("requires and trims both V2 relay bindings", () => {
    expect(
      alertRelayV2Config({
        ALERT_RELAY_V2_URL: " https://relay-v2.test/ ",
        ALERT_RELAY_V2_TOKEN: " token ",
      } as Env),
    ).toEqual({ baseUrl: "https://relay-v2.test/", token: "token" });
    expect(alertRelayV2Config({ ALERT_RELAY_V2_URL: "https://relay-v2.test" } as Env)).toBeNull();
  });

  it.each([
    "http://relay-v2.test",
    "https://user:pass@relay-v2.test",
    "https://relay-v2.test?route=other",
  ])(
    "rejects unsafe V2 relay URL %s",
    (url) => {
      expect(
        alertRelayV2Config({
          ALERT_RELAY_V2_URL: url,
          ALERT_RELAY_V2_TOKEN: "token",
        } as Env),
      ).toBeNull();
      expect(
        hasAlertDestination({
          ALERT_RELAY_V2_URL: url,
          ALERT_RELAY_V2_TOKEN: "token",
        } as Env),
      ).toBe(false);
    },
  );
});

describe("AlertEvent V2", () => {
  it("removes producer environment and Slack text and includes immutable SHA", () => {
    const v2 = createAlertEventV2(event, "a".repeat(40));
    expect(v2).toMatchObject({
      version: "2",
      alert_id: event.alert_id,
      fingerprint: event.fingerprint,
      deployment_sha: "a".repeat(40),
    });
    expect(v2).not.toHaveProperty("environment");
    expect(v2).not.toHaveProperty("slack_text");
  });

  it("omits malformed deployment SHAs", () => {
    expect(createAlertEventV2(event, "not a sha")).not.toHaveProperty("deployment_sha");
  });

  it("truncates large monitor context and redacts secrets before posting", () => {
    const context = createAlertEventV2({
      ...event,
      context: {
        api_token: "secret",
        remote_ip: "192.0.2.1",
        models: Array.from({ length: 60 }, (_, index) => ({
          model_id: `model-${index}`,
          error: `Bearer abcdefghijklmnop ${"x".repeat(4_000)}`,
        })),
        nested: { a: { b: { c: { d: "too deep" } } } },
      },
    }).context;
    const models = context.models as Array<{ model_id: string; error: string }>;
    expect(context.api_token).toBe("[REDACTED]");
    expect(context.remote_ip).toBe("[REDACTED]");
    expect(models.length).toBeGreaterThan(0);
    expect(models.length).toBeLessThanOrEqual(50);
    expect(models[0].error.length).toBeLessThanOrEqual(2_000);
    expect(models[0].error).toContain("Bearer [REDACTED]");
    expect(new TextEncoder().encode(JSON.stringify(context)).byteLength).toBeLessThanOrEqual(
      24_000,
    );
    expect(
      (context.nested as { a: { b: { c: unknown } } }).a.b.c,
    ).toBe("[TRUNCATED]");

    const singleError = createAlertEventV2({
      ...event,
      context: { probe: { error: "x".repeat(4_000) } },
    }).context as { probe: { error: string } };
    expect(singleError.probe.error).toHaveLength(2_000);
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

describe("postAlertEventV2", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("posts only the V2 event to /v2/alerts", async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init: RequestInit) => new Response(null, { status: 202 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const v2 = createAlertEventV2(event, "a".repeat(40));

    await expect(
      postAlertEventV2({ baseUrl: "https://relay-v2.test/", token: "v2-secret" }, v2),
    ).resolves.toBe(true);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://relay-v2.test/v2/alerts");
    expect(init.headers).toEqual({
      Authorization: "Bearer v2-secret",
      "Content-Type": "application/json",
    });
    const payload = JSON.parse(String(init.body));
    expect(payload.version).toBe("2");
    expect(payload).not.toHaveProperty("environment");
    expect(payload).not.toHaveProperty("slack_text");
  });

  it("refuses unsafe relay URLs without making a request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      postAlertEventV2(
        { baseUrl: "https://user:pass@relay-v2.test", token: "v2-secret" },
        createAlertEventV2(event),
      ),
    ).resolves.toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
