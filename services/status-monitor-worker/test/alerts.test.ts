import { afterEach, describe, expect, it, vi } from "vitest";

import {
  decideAlerts,
  deriveEnvironment,
  escapeSlackText,
  formatCycleDownAlert,
  formatModelDownAlert,
  formatModelRecoveredAlert,
  postSlack,
  runAlerts,
  runCycleAlert,
} from "../src/alerts";
import type { StatusMonitorControlPlaneService } from "../src/control-plane";
import type { Config, Env } from "../src/env";
import type { ProbeResult } from "../src/probe";

const VERSION_ID = "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908";

const config = {
  gatewayBaseUrl: "https://staging.freeinference.org",
  targetEnvironment: "staging",
  probePrompt: "hi",
  probeMaxTokens: 32,
  maxConcurrency: 3,
  probeHeader: "synthetic",
  retentionDays: 7,
  probeDeadlineMs: 1000,
  alertFailureThreshold: 2,
  alertStormThreshold: 5,
} as Config;

function result(modelId: string, ok: boolean, error: string | null = ok ? null : "boom"): ProbeResult {
  return {
    modelId,
    ok,
    checkedAt: "2026-06-25T00:00:00Z",
    latencyMs: 1,
    ttftMs: null,
    completionTokens: null,
    throughputTps: null,
    error,
  };
}

describe("deriveEnvironment", () => {
  it("classifies staging, production, local, and unknown hosts", () => {
    expect(deriveEnvironment("https://staging.freeinference.org")).toBe("staging");
    expect(deriveEnvironment("https://freeinference.org")).toBe("production");
    expect(deriveEnvironment("http://localhost:8787")).toBe("local");
    expect(deriveEnvironment("http://127.0.0.1:8080")).toBe("local");
    expect(deriveEnvironment("http://[::1]:8787")).toBe("local"); // IPv6 loopback
    expect(deriveEnvironment("https://freeinference.org:8443")).toBe("production"); // host w/ port
    expect(deriveEnvironment("https://example.com")).toBe("unknown");
    expect(deriveEnvironment("not a url")).toBe("unknown");
  });
});

describe("escapeSlackText", () => {
  it("escapes Slack control characters so untrusted text can't inject mentions", () => {
    expect(escapeSlackText("<!channel> & <@U1>")).toBe("&lt;!channel&gt; &amp; &lt;@U1&gt;");
  });
});

describe("formatModelDownAlert", () => {
  it("includes the model, threshold, escaped error, environment, and gateway", () => {
    const msg = formatModelDownAlert(config, result("gpt-x", false, "upstream <boom>"), 2);
    expect(msg).toContain("\u{1F6A8}");
    expect(msg).toContain("Model down: `gpt-x`");
    expect(msg).toContain("Failed the last 2 probes");
    expect(msg).toContain("upstream &lt;boom&gt;"); // escaped
    expect(msg).toContain("staging");
    expect(msg).toContain("https://staging.freeinference.org");
  });

  it("renders a placeholder when the error is null", () => {
    expect(formatModelDownAlert(config, result("m", false, null), 2)).toContain("(no error message)");
  });
});

describe("formatModelRecoveredAlert", () => {
  it("names the recovered model", () => {
    const msg = formatModelRecoveredAlert(config, result("gpt-x", true));
    expect(msg).toContain("✅");
    expect(msg).toContain("Model recovered: `gpt-x`");
  });
});

describe("decideAlerts", () => {
  it("alerts down only on the transition into the failing set", () => {
    const first = decideAlerts([result("a", false)], new Set(["a"]), {});
    expect(first.down.map((r) => r.modelId)).toEqual(["a"]);

    // Already alerted (state present) — a sustained outage must not page again.
    const second = decideAlerts([result("a", false)], new Set(["a"]), { a: "2026-06-25T00:00:00Z" });
    expect(second.down).toEqual([]);
    expect(second.recovered).toEqual([]);
  });

  it("does not alert a model that is failing but below the threshold", () => {
    // Failing this cycle but not in the >=threshold set yet.
    const d = decideAlerts([result("a", false)], new Set(), {});
    expect(d.down).toEqual([]);
    expect(d.baseState).toEqual({});
  });

  it("emits a recovery alert when a down model succeeds (state cleared by caller on send)", () => {
    const d = decideAlerts([result("a", true)], new Set(), { a: "2026-06-25T00:00:00Z" });
    expect(d.recovered.map((r) => r.modelId)).toEqual(["a"]);
    // decideAlerts is pure: it does not clear state — runAlerts does, only once
    // the recovery POST is confirmed delivered.
    expect(d.baseState.a).toBe("2026-06-25T00:00:00Z");
  });

  it("drops base state for models no longer probed this cycle", () => {
    const d = decideAlerts([result("a", true)], new Set(), { gone: "2026-06-25T00:00:00Z" });
    expect(d.baseState.gone).toBeUndefined();
  });
});

describe("postSlack", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("never logs the webhook URL when the request throws", async () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const webhook = "https://hooks.slack.com/services/T000/B000/secret-part";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        // Workers fetch failures embed the request URL like this — the webhook
        // URL is the credential, so it must never reach the log call.
        throw new Error(`Fetch API cannot load: ${webhook}`);
      }),
    );

    expect(await postSlack(webhook, "hi")).toBe(false);
    expect(log).toHaveBeenCalledWith("slack webhook post failed");
    for (const call of log.mock.calls) {
      expect(JSON.stringify(call)).not.toContain("hooks.slack.com");
    }
  });

  it("returns true on a 2xx and posts the text payload", async () => {
    const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toEqual({ text: "hello" });
      return new Response("ok", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    expect(await postSlack("https://hook.test/x", "hello")).toBe(true);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("returns false on a non-2xx", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("no", { status: 500 })));
    expect(await postSlack("https://hook.test/x", "hello")).toBe(false);
  });

  it("returns false when the request throws", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network");
      }),
    );
    expect(await postSlack("https://hook.test/x", "hello")).toBe(false);
  });
});

// --- Minimal in-memory D1 double covering only the queries the alert path runs.
class FakeStmt {
  args: unknown[] = [];
  constructor(
    private db: FakeD1,
    private sql: string,
  ) {}
  bind(...args: unknown[]): this {
    this.args = args;
    return this;
  }
  async first<T>(): Promise<T | null> {
    if (/SELECT value FROM meta WHERE key = \?/.test(this.sql)) {
      const v = this.db.meta.get(this.args[0] as string);
      return v == null ? null : ({ value: v } as T);
    }
    if (/SELECT 1 AS present FROM meta WHERE key GLOB \? AND value = \? LIMIT 1/.test(this.sql)) {
      const prefix = (this.args[0] as string).slice(0, -1);
      const value = this.args[1] as string;
      const found = [...this.db.meta].some(
        ([key, stored]) => key.startsWith(prefix) && stored === value,
      );
      return found ? ({ present: 1 } as T) : null;
    }
    throw new Error(`unhandled first(): ${this.sql}`);
  }
  async run(): Promise<{ meta: { changes: number } }> {
    if (/INSERT OR REPLACE INTO meta/.test(this.sql)) {
      this.db.meta.set(this.args[0] as string, this.args[1] as string);
      return { meta: { changes: 1 } };
    }
    if (/DELETE FROM meta WHERE key = \?/.test(this.sql)) {
      const existed = this.db.meta.delete(this.args[0] as string);
      return { meta: { changes: existed ? 1 : 0 } };
    }
    throw new Error(`unhandled run(): ${this.sql}`);
  }
  async all<T>(): Promise<{ results: T[] }> {
    if (
      /SELECT ok FROM probe_results\s+WHERE model_id = \? AND target_environment = \?\s+ORDER BY id DESC LIMIT \?/.test(
        this.sql,
      )
    ) {
      const modelId = this.args[0] as string;
      const target = this.args[1] as string;
      const limit = this.args[2] as number;
      const rows = this.db.probe
        .filter(
          (r) => r.model_id === modelId && (r.target_environment ?? "staging") === target,
        )
        .sort((a, b) => b.id - a.id)
        .slice(0, limit)
        .map((r) => ({ ok: r.ok }));
      return { results: rows as T[] };
    }
    if (/SELECT key, value FROM meta WHERE key GLOB \? ORDER BY key/.test(this.sql)) {
      const prefix = (this.args[0] as string).slice(0, -1);
      const rows = [...this.db.meta]
        .filter(([key]) => key.startsWith(prefix))
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, value]) => ({ key, value }));
      return { results: rows as T[] };
    }
    throw new Error(`unhandled all(): ${this.sql}`);
  }

  isRead(): boolean {
    return /SELECT ok FROM probe_results/.test(this.sql);
  }
}

class FakeD1 {
  // `target_environment` is optional here so seeds written before the column
  // existed keep meaning what they did; the fake reads them as staging, exactly
  // as the backfilled rows do.
  probe: Array<{
    id: number;
    model_id: string;
    ok: number;
    target_environment?: string;
  }> = [];
  meta = new Map<string, string>();
  failBatchAfter: number | null = null;
  private seq = 0;
  prepare(sql: string): FakeStmt {
    return new FakeStmt(this, sql);
  }
  async batch<T>(stmts: FakeStmt[]): Promise<Array<{ results: T[] }>> {
    const snapshot = new Map(this.meta);
    const results: Array<{ results: T[] }> = [];
    try {
      for (let index = 0; index < stmts.length; index++) {
        if (this.failBatchAfter === index) throw new Error("injected batch failure");
        const stmt = stmts[index];
        if (stmt.isRead()) {
          results.push(await stmt.all<T>());
        } else {
          await stmt.run();
          results.push({ results: [] });
        }
      }
      return results;
    } catch (error) {
      this.meta = snapshot;
      this.failBatchAfter = null;
      throw error;
    }
  }
  record(modelId: string, ok: boolean, targetEnvironment = "staging"): void {
    this.probe.push({
      id: ++this.seq,
      model_id: modelId,
      ok: ok ? 1 : 0,
      target_environment: targetEnvironment,
    });
  }

  /** Mirrors reconcileModels: drops rows for models not probed this cycle. */
  reconcile(activeIds: string[]): void {
    const active = new Set(activeIds);
    this.probe = this.probe.filter((r) => active.has(r.model_id));
  }
}

function stubFetch(): Array<{ text: string }> {
  const posts: Array<{ text: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit) => {
      posts.push(JSON.parse(String(init.body)));
      return new Response("ok", { status: 200 });
    }),
  );
  return posts;
}

function cfg(threshold: number, storm: number = config.alertStormThreshold): Config {
  return { ...config, alertFailureThreshold: threshold, alertStormThreshold: storm };
}

function acceptedRpcResult() {
  return {
    accepted: true as const,
    acknowledgement: {
      accepted: true as const,
      incident_id: "incident-status-monitor",
      generation: 1,
      lifecycle_state: "firing",
      action: "accepted",
      occurrence_count: 1,
      state_version: 1,
    },
  };
}

function controlPlaneEnv(
  db: FakeD1,
  submitStatusMonitorEvent: StatusMonitorControlPlaneService["submitStatusMonitorEvent"],
  defaultOwner = "control-plane",
  webhook?: string,
): Env {
  return {
    DB: db as unknown as D1Database,
    ALERT_CONTROL_PLANE: { submitStatusMonitorEvent },
    CF_VERSION_METADATA: { id: VERSION_ID } as WorkerVersionMetadata,
    ALERT_DEFAULT_OWNER: defaultOwner,
    SLACK_WEBHOOK_URL: webhook,
  } as unknown as Env;
}

/** Records one cycle's results into the fake DB (reconciling as the worker does) and evaluates alerts. */
async function cycle(
  db: FakeD1,
  env: Env,
  perModel: Record<string, boolean>,
  conf: Config = config,
): Promise<void> {
  const results = Object.entries(perModel).map(([id, ok]) => result(id, ok));
  for (const r of results) db.record(r.modelId, r.ok);
  db.reconcile(results.map((r) => r.modelId));
  await runAlerts(env, conf, results);
}

describe("runAlerts", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function envWith(
    db: FakeD1,
    webhook: string | undefined,
    relayUrl?: string,
    relayToken?: string,
  ): Env {
    return {
      SLACK_WEBHOOK_URL: webhook,
      CODEX_ONCALL_RELAY_URL: relayUrl,
      CODEX_ONCALL_RELAY_TOKEN: relayToken,
      DB: db as unknown as D1Database,
    } as unknown as Env;
  }

  it("sends an authenticated oncall event and skips Slack when the relay succeeds", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x", "https://relay.test/", "relay-token");
    const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => {
      return new Response(null, { status: 202 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await cycle(db, env, { a: false }, cfg(1));

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://relay.test/v1/alerts");
    expect(init.headers).toEqual({
      Authorization: "Bearer relay-token",
      "Content-Type": "application/json",
    });
    const payload = JSON.parse(String(init.body));
    expect(payload).toMatchObject({
      version: "1",
      fingerprint: "status-monitor:model:a",
      source: "status-monitor-worker",
      status: "firing",
      severity: "error",
      title: "Model down: a",
      environment: "staging",
      occurred_at: "2026-06-25T00:00:00Z",
      context: {
        alert_type: "model",
        gateway_base_url: "https://staging.freeinference.org",
        failure_threshold: 1,
        probe: { model_id: "a", ok: false, error: "boom" },
      },
    });
    expect(payload.alert_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(payload.summary).toContain("a failed 1 consecutive probes");
    expect(payload.slack_text).toContain("Model down: `a`");
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
  });

  it("uses the same model fingerprint for relay firing and recovery events", async () => {
    const db = new FakeD1();
    const env = envWith(db, undefined, "https://relay.test", "relay-token");
    const events: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        events.push(JSON.parse(String(init.body)));
        return new Response(null, { status: 200 });
      }),
    );

    await cycle(db, env, { a: false }, cfg(1));
    await cycle(db, env, { a: true }, cfg(1));

    expect(events.map((event) => event.status)).toEqual(["firing", "resolved"]);
    expect(events.map((event) => event.fingerprint)).toEqual([
      "status-monitor:model:a",
      "status-monitor:model:a",
    ]);
    expect(events.map((event) => event.severity)).toEqual(["error", "info"]);
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
  });

  it("sends new individual firing and recovery transitions only through the control plane", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);
    const legacyFetch = vi.fn();
    vi.stubGlobal("fetch", legacyFetch);

    await cycle(db, env, { a: false }, cfg(1));
    expect(JSON.parse(bodies[0])).toMatchObject({
      alert_type: "model_unavailable",
      fingerprint: "status-monitor:model:a",
      status: "firing",
    });
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty(
      "a",
      "status-monitor:model:a",
    );
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:a")).toBe(
      "control-plane",
    );
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(false);

    await cycle(db, env, { a: true }, cfg(1));
    expect(bodies.map((body) => JSON.parse(body).status)).toEqual(["firing", "resolved"]);
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);
    expect(
      db.meta.has("alert_delivery_pending:v1:resolved:status-monitor:model:a"),
    ).toBe(false);
    expect(legacyFetch).not.toHaveBeenCalled();
  });

  it("retries the exact pending body after an ambiguous RPC without legacy fallback", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      if (bodies.length === 1) throw new Error("response lost");
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit, "control-plane", "https://hook.test/x");
    const legacyFetch = vi.fn();
    vi.stubGlobal("fetch", legacyFetch);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    await cycle(db, env, { a: false }, cfg(1));
    expect(db.meta.has("alert_state")).toBe(false);
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(true);

    await cycle(db, env, { a: false }, cfg(1));
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
    expect(JSON.parse(bodies[1]).event_id).toBe(JSON.parse(bodies[0]).event_id);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
    expect(legacyFetch).not.toHaveBeenCalled();
  });

  it("retries a pending firing before recording a later recovery", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      if (bodies.length === 1) throw new Error("response lost");
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    await cycle(db, env, { a: false }, cfg(1));
    await cycle(db, env, { a: true }, cfg(1));

    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty(
      "a",
      "status-monitor:model:a",
    );
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(false);

    await cycle(db, env, { a: true }, cfg(1));
    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "firing",
      "resolved",
    ]);
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
  });

  it("closes a replayed firing when the model left the catalog", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      if (bodies.length === 1) throw new Error("response lost");
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    // A departure is only inferable from a catalog the cycle actually observed,
    // so `a` leaves a still-populated catalog rather than an empty one.
    await cycle(db, env, { a: false }, cfg(1));
    await cycle(db, env, { b: true }, cfg(1));
    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "firing",
    ]);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty(
      "a",
      "status-monitor:model:a",
    );
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(false);

    await cycle(db, env, { b: true }, cfg(1));
    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "firing",
      "resolved",
    ]);
    const alertState = db.meta.has("alert_state")
      ? JSON.parse(db.meta.get("alert_state")!)
      : {};
    expect(alertState).not.toHaveProperty("a");
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(false);
    expect(
      db.meta.has("alert_delivery_pending:v1:resolved:status-monitor:model:a"),
    ).toBe(false);
  });

  it("closes an active control-plane incident when the model left the catalog", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);

    await cycle(db, env, { a: false }, cfg(1));
    await cycle(db, env, { b: true }, cfg(1));

    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "resolved",
    ]);
    expect(JSON.parse(bodies[1])).toMatchObject({
      title: "Model monitoring ended: a",
      context: { model_id: "a" },
    });
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);
  });

  it("retries the exact departure resolution after an ambiguous response", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      if (bodies.length === 2) throw new Error("response lost");
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    await cycle(db, env, { a: false }, cfg(1));
    await cycle(db, env, { b: true }, cfg(1));

    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty(
      "a",
      "status-monitor:model:a",
    );
    expect(
      db.meta.has("alert_delivery_pending:v1:resolved:status-monitor:model:a"),
    ).toBe(true);

    await cycle(db, env, { b: true }, cfg(1));

    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "resolved",
      "resolved",
    ]);
    expect(bodies[2]).toBe(bodies[1]);
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);
  });

  it("retries a pending resolution before opening a later outage", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      if (bodies.length === 2) throw new Error("response lost");
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    await cycle(db, env, { a: false }, cfg(1));
    await cycle(db, env, { a: true }, cfg(1));
    await cycle(db, env, { a: false }, cfg(1));

    expect(bodies).toHaveLength(3);
    expect(bodies[2]).toBe(bodies[1]);
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
    expect(
      db.meta.has("alert_delivery_pending:v1:resolved:status-monitor:model:a"),
    ).toBe(false);

    await cycle(db, env, { a: false }, cfg(1));
    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "resolved",
      "resolved",
      "firing",
    ]);
    expect(JSON.parse(bodies[3]).event_id).not.toBe(JSON.parse(bodies[0]).event_id);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty(
      "a",
      "status-monitor:model:a",
    );
  });

  it("keeps a pending control-plane event out of a later legacy storm summary", async () => {
    const db = new FakeD1();
    const submit = vi.fn(async () => {
      throw new Error("response lost");
    });
    // `a` opens under the control-plane default and its transition sticks as
    // durable pending. After a rollback to the legacy default, the storm
    // summary of the new legacy incidents must not double-write `a`, whose
    // transition still belongs to the control plane.
    const envControlPlane = controlPlaneEnv(db, submit, "control-plane", "https://hook.test/x");
    const env = controlPlaneEnv(db, submit, "legacy", "https://hook.test/x");
    const posts = stubFetch();
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    await cycle(db, envControlPlane, { a: false }, cfg(1, 2));
    await cycle(db, env, { a: false, b: false, c: false, d: false }, cfg(1, 2));

    expect(submit).toHaveBeenCalledTimes(2);
    expect(posts).toHaveLength(1);
    expect(posts[0].text).not.toContain("`a`");
    expect(posts[0].text).toContain("`b`");
    expect(posts[0].text).toContain("`c`");
    expect(posts[0].text).toContain("`d`");
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(true);
  });

  it("drains a pre-cutover recovery with no owner through the legacy writer", async () => {
    const db = new FakeD1();
    db.meta.set("alert_state", JSON.stringify({ a: "2026-06-25T00:00:00Z" }));
    const submit = vi.fn(async (_bodyJson: string) => acceptedRpcResult());
    const env = controlPlaneEnv(db, submit, "control-plane", "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: true }, cfg(1));

    expect(submit).not.toHaveBeenCalled();
    expect(posts).toHaveLength(1);
    expect(posts[0].text).toContain("Model recovered: `a`");
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);
  });

  it("uses the legacy writer for new incidents after the default is rolled back", async () => {
    const db = new FakeD1();
    const submit = vi.fn(async (_bodyJson: string) => acceptedRpcResult());
    const env = controlPlaneEnv(db, submit, "legacy", "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false }, cfg(1));

    expect(submit).not.toHaveBeenCalled();
    expect(posts).toHaveLength(1);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:a")).toBe("legacy");
  });

  it("fails closed without fallback when control-plane ownership is configured but its binding is absent", async () => {
    const db = new FakeD1();
    const env = envWith(db, undefined);
    env.ALERT_DEFAULT_OWNER = "control-plane";
    const legacyFetch = vi.fn();
    vi.stubGlobal("fetch", legacyFetch);

    await cycle(db, env, { a: false }, cfg(1));

    expect(legacyFetch).not.toHaveBeenCalled();
    expect(db.meta.has("alert_state")).toBe(false);
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(true);
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:a")).toBe(
      "control-plane",
    );
  });

  it("preserves a control-plane recovery after rollback when its binding is absent", async () => {
    const db = new FakeD1();
    const submit = vi.fn(async () => acceptedRpcResult());
    await cycle(db, controlPlaneEnv(db, submit), { a: false }, cfg(1));

    const rolledBack = envWith(db, undefined);
    rolledBack.ALERT_DEFAULT_OWNER = "legacy";
    const legacyFetch = vi.fn();
    vi.stubGlobal("fetch", legacyFetch);

    await cycle(db, rolledBack, { a: true }, cfg(1));

    expect(legacyFetch).not.toHaveBeenCalled();
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
    expect(
      db.meta.has("alert_delivery_pending:v1:resolved:status-monitor:model:a"),
    ).toBe(true);
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:a")).toBe(
      "control-plane",
    );
  });

  it("does not pin unrelated legacy incidents when no legacy destination exists", async () => {
    const db = new FakeD1();
    db.meta.set("alert_state", JSON.stringify({ active: "status-monitor:model:active" }));
    db.meta.set(
      "alert_delivery_owner:v1:status-monitor:model:active",
      "control-plane",
    );
    const env = envWith(db, undefined);
    env.ALERT_DEFAULT_OWNER = "legacy";

    await cycle(db, env, { active: false, new_model: false }, cfg(1));

    expect(JSON.parse(db.meta.get("alert_state")!)).toEqual({
      active: "status-monitor:model:active",
    });
    expect(
      db.meta.has("alert_delivery_owner:v1:status-monitor:model:new_model"),
    ).toBe(false);
  });

  it("fails closed without fallback when a bound control-plane deployment identity is invalid", async () => {
    const db = new FakeD1();
    const submit = vi.fn(async () => acceptedRpcResult());
    const env = {
      ...controlPlaneEnv(db, submit, "control-plane", "https://hook.test/x"),
      CF_VERSION_METADATA: undefined,
    } as unknown as Env;
    const legacyFetch = vi.fn();
    vi.stubGlobal("fetch", legacyFetch);

    await cycle(db, env, { a: false }, cfg(1));

    expect(submit).not.toHaveBeenCalled();
    expect(legacyFetch).not.toHaveBeenCalled();
    expect(db.meta.has("alert_state")).toBe(false);
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(true);
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:a")).toBe(
      "control-plane",
    );
  });

  it("finishes an existing control-plane incident there after rollback", async () => {
    const db = new FakeD1();
    db.meta.set(
      "alert_state",
      JSON.stringify({ a: "status-monitor:model:a" }),
    );
    db.meta.set("alert_delivery_owner:v1:status-monitor:model:a", "control-plane");
    const submit = vi.fn(async (_bodyJson: string) => acceptedRpcResult());
    const env = controlPlaneEnv(db, submit, "legacy", "https://hook.test/x");
    const legacyFetch = vi.fn();
    vi.stubGlobal("fetch", legacyFetch);

    await cycle(db, env, { a: true }, cfg(1));

    expect(submit).toHaveBeenCalledOnce();
    expect(JSON.parse(String(submit.mock.calls[0][0])).status).toBe("resolved");
    expect(legacyFetch).not.toHaveBeenCalled();
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);
  });

  // D1 decision (2026-07-27): control-plane-owned mass outages open one
  // incident per model instead of collapsing into a legacy summary. This also
  // closes the old storm black hole — with no legacy destination configured, a
  // mass outage now delivers through the Control Plane instead of logging
  // "undeliverable" every cycle.
  it("opens one control-plane incident per model in a mass outage", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit); // no relay, no webhook
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const conf = cfg(1, 2); // 3 models > storm threshold of 2

    await cycle(db, env, { a: false, b: false, c: false }, conf);

    expect(submit).toHaveBeenCalledTimes(3);
    expect(
      bodies.map((body) => JSON.parse(body).context.model_id).sort(),
    ).toEqual(["a", "b", "c"]);
    expect(log).not.toHaveBeenCalled(); // delivered — nothing is undeliverable
    const state = JSON.parse(db.meta.get("alert_state")!);
    expect(Object.keys(state).sort()).toEqual(["a", "b", "c"]);
    expect(state.a).toBe("status-monitor:model:a"); // individual, not storm, fingerprints
  });

  it("recovers mass-outage models independently instead of all-or-nothing", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);
    const conf = cfg(1, 2);

    await cycle(db, env, { a: false, b: false, c: false }, conf);
    // Only `a` recovers. Under the legacy storm grouping this resolved nothing
    // until the whole group came back; per-model incidents resolve one by one.
    await cycle(db, env, { a: true, b: false, c: false }, conf);

    const transitions = bodies.map((body) => {
      const parsed = JSON.parse(body);
      return `${parsed.status}:${parsed.context.model_id}`;
    });
    expect(transitions.slice(3).sort()).toEqual(["resolved:a"]);
    const state = JSON.parse(db.meta.get("alert_state")!);
    expect(Object.keys(state).sort()).toEqual(["b", "c"]);
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:b")).toBe("control-plane");
  });

  it("keeps the storm summary for the legacy rollback mode", async () => {
    const db = new FakeD1();
    const submit = vi.fn(async () => acceptedRpcResult());
    // ALERT_DEFAULT_OWNER rolled back to legacy: one webhook text per model
    // would flood the channel, so the collapse must survive there.
    const env = controlPlaneEnv(db, submit, "legacy", "https://hook.test/x");
    const posts = stubFetch();
    const conf = cfg(1, 2);

    await cycle(db, env, { a: false, b: false, c: false }, conf);
    await cycle(db, env, { a: true, b: true, c: true }, conf);

    expect(submit).not.toHaveBeenCalled();
    expect(posts).toHaveLength(2);
    expect(posts[0].text).toContain("3 models down");
    expect(posts[1].text).toContain("3 models recovered");
    expect(
      [...db.meta.keys()].some((key) => key.startsWith("alert_delivery_owner:v1:")),
    ).toBe(false);
  });

  it("drains a pre-switch storm through the legacy summary after the cutover", async () => {
    const db = new FakeD1();
    const submit = vi.fn(async () => acceptedRpcResult());
    const env = controlPlaneEnv(db, submit, "control-plane", "https://hook.test/x");
    const posts = stubFetch();
    const conf = cfg(1, 2);
    // A storm opened before the per-model switch left storm-fingerprint state;
    // its incident belongs to the legacy writer until it fully recovers.
    db.meta.set(
      "alert_state",
      JSON.stringify({
        a: "status-monitor:storm:cafe1234",
        b: "status-monitor:storm:cafe1234",
      }),
    );

    await cycle(db, env, { a: true, b: true }, conf);

    expect(submit).not.toHaveBeenCalled();
    expect(posts).toHaveLength(1);
    expect(posts[0].text).toContain("2 models recovered");
    const state = db.meta.has("alert_state")
      ? JSON.parse(db.meta.get("alert_state")!)
      : {};
    expect(Object.keys(state)).toEqual([]);
  });

  it("atomically retains state, pending event, and owner when completion commit fails", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);
    db.failBatchAfter = 1;

    await expect(cycle(db, env, { a: false }, cfg(1))).rejects.toThrow(
      "injected batch failure",
    );
    expect(db.meta.has("alert_state")).toBe(false);
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(true);
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:a")).toBe(
      "control-plane",
    );

    await cycle(db, env, { a: false }, cfg(1));
    expect(bodies[1]).toBe(bodies[0]);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:model:a"),
    ).toBe(false);
  });

  it("atomically retains a resolved pending event and its owner when release fails", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);
    await cycle(db, env, { a: false }, cfg(1));
    db.failBatchAfter = 2;

    await expect(cycle(db, env, { a: true }, cfg(1))).rejects.toThrow(
      "injected batch failure",
    );
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
    expect(
      db.meta.has("alert_delivery_pending:v1:resolved:status-monitor:model:a"),
    ).toBe(true);
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:a")).toBe(
      "control-plane",
    );

    await cycle(db, env, { a: true }, cfg(1));
    expect(bodies[2]).toBe(bodies[1]);
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
    expect(
      db.meta.has("alert_delivery_pending:v1:resolved:status-monitor:model:a"),
    ).toBe(false);
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);
  });

  it("falls back to Slack and commits state when the relay returns non-2xx", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x", "https://relay.test", "relay-token");
    const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        requests.push({ url, body: JSON.parse(String(init.body)) });
        return url.includes("relay.test")
          ? new Response(null, { status: 503 })
          : new Response("ok", { status: 200 });
      }),
    );

    await cycle(db, env, { a: false }, cfg(1));

    expect(requests.map((request) => request.url)).toEqual([
      "https://relay.test/v1/alerts",
      "https://hook.test/x",
    ]);
    expect(requests[1].body.text).toContain("Model down: `a`");
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
  });

  it("falls back to Slack when the relay request times out", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x", "https://relay.test", "relay-token");
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        urls.push(url);
        if (url.includes("relay.test")) throw new DOMException("timed out", "TimeoutError");
        return new Response("ok", { status: 200 });
      }),
    );

    await cycle(db, env, { a: false }, cfg(1));

    expect(urls).toEqual(["https://relay.test/v1/alerts", "https://hook.test/x"]);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
  });

  it("does not commit state when both the relay and Slack fallback fail", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x", "https://relay.test", "relay-token");
    const fetchMock = vi.fn(async () => new Response(null, { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);

    await cycle(db, env, { a: false }, cfg(1));

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const state = db.meta.has("alert_state") ? JSON.parse(db.meta.get("alert_state")!) : {};
    expect(state).not.toHaveProperty("a");
  });

  it("pages once on the second consecutive failure, not on the first or third", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false }); // 1st failure → no page
    expect(posts).toHaveLength(0);

    await cycle(db, env, { a: false }); // 2nd failure → page
    expect(posts).toHaveLength(1);
    expect(posts[0].text).toContain("Model down: `a`");
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");

    await cycle(db, env, { a: false }); // 3rd failure → still paged, no repeat
    expect(posts).toHaveLength(1);
  });

  it("does not count another deployment's failures toward the streak", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    // A failure this Worker never observed: the row predates the cutover that
    // repointed it, and belongs to the gateway it used to probe.
    db.record("a", false, "production");

    // With the retained row counted, this first observed failure would be the
    // second in a row and would page immediately.
    await cycle(db, env, { a: false });

    expect(posts).toHaveLength(0);
  });

  it("posts a recovery notice and re-arms after the model comes back", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false });
    await cycle(db, env, { a: false }); // down page (1)
    await cycle(db, env, { a: true }); // recovery (2)
    expect(posts).toHaveLength(2);
    expect(posts[1].text).toContain("Model recovered: `a`");
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");

    await cycle(db, env, { a: false });
    await cycle(db, env, { a: false }); // new outage → page again (3)
    expect(posts).toHaveLength(3);
    expect(posts[2].text).toContain("Model down: `a`");
  });

  it("tracks models independently", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false, b: true });
    await cycle(db, env, { a: false, b: true }); // only a is down twice
    expect(posts).toHaveLength(1);
    expect(posts[0].text).toContain("`a`");
  });

  it("does not commit alert state when the Slack POST fails, and retries next cycle", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    let status = 500;
    const posts: Array<{ text: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        posts.push(JSON.parse(String(init.body)));
        return new Response("x", { status });
      }),
    );

    await cycle(db, env, { a: false });
    await cycle(db, env, { a: false }); // 2nd failure → POST attempted but 500
    expect(posts).toHaveLength(1);
    // A failed page must not be recorded as sent, or the outage would never re-page.
    const stateAfterFail = db.meta.has("alert_state") ? JSON.parse(db.meta.get("alert_state")!) : {};
    expect(stateAfterFail.a).toBeUndefined();

    status = 200;
    await cycle(db, env, { a: false }); // retried → delivered this time
    expect(posts).toHaveLength(2);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
  });

  it("does nothing when no destination is configured", async () => {
    const db = new FakeD1();
    const env = envWith(db, undefined);
    const posts = stubFetch();

    await cycle(db, env, { a: false });
    await cycle(db, env, { a: false });
    expect(posts).toHaveLength(0);
    expect(db.meta.has("alert_state")).toBe(false);
  });

  it("honors a threshold of 1 (page on the first failure)", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false }, cfg(1));
    expect(posts).toHaveLength(1);
    expect(posts[0].text).toContain("Failed the last 1 probes");
  });

  it("honors a threshold of 3 (no page until the third consecutive failure)", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false }, cfg(3));
    await cycle(db, env, { a: false }, cfg(3));
    expect(posts).toHaveLength(0); // only two in a row
    await cycle(db, env, { a: false }, cfg(3));
    expect(posts).toHaveLength(1);
    expect(posts[0].text).toContain("Failed the last 3 probes");
  });

  it("re-arms cleanly from a corrupt alert_state value", async () => {
    const db = new FakeD1();
    db.meta.set("alert_state", "{not valid json"); // e.g. a half-written row
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false });
    await cycle(db, env, { a: false }); // corrupt state treated as empty → still pages
    expect(posts).toHaveLength(1);
    expect(JSON.parse(db.meta.get("alert_state")!)).toHaveProperty("a");
  });

  it("recovers one model while another stays down (multi-model partial recovery)", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false, b: false });
    await cycle(db, env, { a: false, b: false }); // both page down (2)
    expect(posts).toHaveLength(2);

    await cycle(db, env, { a: true, b: false }); // a recovers, b still down
    expect(posts).toHaveLength(3);
    expect(posts[2].text).toContain("Model recovered: `a`");
    const state = JSON.parse(db.meta.get("alert_state")!);
    expect(state).not.toHaveProperty("a");
    expect(state).toHaveProperty("b"); // b's down state persists, not re-paged
  });

  it("never pages recovery for a model that was never down", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: true });
    await cycle(db, env, { a: false }); // single failure, below threshold
    await cycle(db, env, { a: true }); // back up — but never alerted down
    expect(posts).toHaveLength(0);
  });

  it("clears state without a false recovery when a down model leaves the catalog", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await cycle(db, env, { a: false });
    await cycle(db, env, { a: false }); // a paged down (1)
    expect(posts).toHaveLength(1);

    await cycle(db, env, { b: true }); // a absent from the catalog this cycle
    expect(posts).toHaveLength(1); // no recovery page for the departed model
    expect(JSON.parse(db.meta.get("alert_state")!)).not.toHaveProperty("a");
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:model:a")).toBe(false);

    // a returns and fails once: its history was reconciled away, so it's not yet
    // a confirmed streak and must not page until it fails twice anew.
    await cycle(db, env, { a: false });
    expect(posts).toHaveLength(1);
    await cycle(db, env, { a: false });
    expect(posts).toHaveLength(2);
  });

  it("is a no-op for an empty result set", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await runAlerts(env, config, []);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(db.meta.has("alert_state")).toBe(false);
  });

  it("never resolves open incidents when the cycle observed no catalog at all", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const env = controlPlaneEnv(db, submit);

    await cycle(db, env, { a: false }, cfg(1));
    await cycle(db, env, { b: false }, cfg(1));
    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "resolved", // a genuinely left a catalog that was still observed
      "firing",
    ]);

    // An empty result set is evidence about nothing. `discoverModels` fails the
    // cycle before this point, so reaching here means something upstream broke —
    // resolving every open incident would be a mass false recovery.
    await runAlerts(env, cfg(1), []);

    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "resolved",
      "firing",
    ]);
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:model:b")).toBe(
      "control-plane",
    );
    // The mapping must survive too. Dropping it would strand b's open incident:
    // a later healthy probe would no longer count as a recovery, so nothing
    // could ever resolve it.
    expect(JSON.parse(db.meta.get("alert_state")!)).toEqual({
      b: "status-monitor:model:b",
    });

    // Proof it is not stranded: b recovers normally on the next observed cycle.
    await cycle(db, env, { b: true }, cfg(1));
    expect(bodies.map((body) => JSON.parse(body).status)).toEqual([
      "firing",
      "resolved",
      "firing",
      "resolved",
    ]);
  });

  it("collapses a mass outage into a single summary page above the storm threshold", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();
    const conf = cfg(2, 3); // summarize when more than 3 models change state
    const down = { a: false, b: false, c: false, d: false, e: false }; // 5 > 3

    await cycle(db, env, down, conf);
    await cycle(db, env, down, conf); // all five cross the threshold this cycle
    expect(posts).toHaveLength(1); // one summary, not five
    expect(posts[0].text).toContain("5 models down");
    expect(posts[0].text).toContain("`a`");
    // Every summarized model is recorded as alerted, so none re-pages next cycle.
    const state = JSON.parse(db.meta.get("alert_state")!);
    expect(Object.keys(state).sort()).toEqual(["a", "b", "c", "d", "e"]);

    await cycle(db, env, down, conf);
    expect(posts).toHaveLength(1); // sustained outage doesn't repeat
  });

  it("resolves a storm with its original relay fingerprint once the whole group recovers", async () => {
    const db = new FakeD1();
    const env = envWith(db, undefined, "https://relay.test", "relay-token");
    const events: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        events.push(JSON.parse(String(init.body)));
        return new Response(null, { status: 202 });
      }),
    );
    const conf = cfg(1, 2);

    await cycle(db, env, { a: false, b: false, c: false }, conf);
    const firingFingerprint = String(events[0].fingerprint);
    expect(events).toHaveLength(1);
    expect(firingFingerprint).toMatch(/^status-monitor:storm:/);
    expect(Object.values(JSON.parse(db.meta.get("alert_state")!))).toEqual([
      firingFingerprint,
      firingFingerprint,
      firingFingerprint,
    ]);

    await cycle(db, env, { a: true, b: false, c: false }, conf);
    expect(events).toHaveLength(1);

    await cycle(db, env, { a: true, b: true, c: true }, conf);
    expect(events).toHaveLength(2);
    expect(events.map((event) => event.status)).toEqual(["firing", "resolved"]);
    expect(events.map((event) => event.fingerprint)).toEqual([
      firingFingerprint,
      firingFingerprint,
    ]);
    expect(JSON.parse(db.meta.get("alert_state")!)).toEqual({});
  });

  it("pages individually at or below the storm threshold", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();
    const conf = cfg(2, 3);
    const down = { a: false, b: false, c: false }; // 3, not > 3

    await cycle(db, env, down, conf);
    await cycle(db, env, down, conf);
    expect(posts).toHaveLength(3); // one message per model
    expect(posts.every((p) => p.text.includes("Model down"))).toBe(true);
  });
});

describe("formatCycleDownAlert", () => {
  it("describes the gateway-level failure with the escaped error", () => {
    const msg = formatCycleDownAlert(config, {
      ok: false,
      checkedAt: "2026-06-25T00:00:00Z",
      error: "models discovery failed: HTTP 503 <x>",
    });
    expect(msg).toContain("Monitoring cycle failing");
    expect(msg).toContain("HTTP 503 &lt;x&gt;");
    expect(msg).toContain("no models could be probed");
  });
});

describe("runCycleAlert", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function envWith(
    db: FakeD1,
    webhook: string | undefined,
    relayUrl?: string,
    relayToken?: string,
  ): Env {
    return {
      SLACK_WEBHOOK_URL: webhook,
      CODEX_ONCALL_RELAY_URL: relayUrl,
      CODEX_ONCALL_RELAY_TOKEN: relayToken,
      DB: db as unknown as D1Database,
    } as unknown as Env;
  }
  const failing = { ok: false, checkedAt: "2026-06-25T00:00:00Z", error: "gateway down" };
  const healthy = { ok: true, checkedAt: "2026-06-25T00:20:00Z", error: null };

  function cycleControlPlaneEnv(
    db: FakeD1,
    submit: StatusMonitorControlPlaneService["submitStatusMonitorEvent"],
  ): Env {
    return {
      DB: db as unknown as D1Database,
      ALERT_CONTROL_PLANE: { submitStatusMonitorEvent: submit },
      CF_VERSION_METADATA: { id: VERSION_ID } as WorkerVersionMetadata,
      ALERT_CYCLE_OWNER: "control-plane",
    } as unknown as Env;
  }

  it("opens and resolves a control-plane cycle incident with atomic state", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const env = cycleControlPlaneEnv(db, submit);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await runCycleAlert(env, config, failing);
    expect(fetchMock).not.toHaveBeenCalled(); // no legacy post for a cp-owned cycle
    expect(JSON.parse(bodies[0])).toMatchObject({
      alert_type: "monitoring_cycle_failure",
      fingerprint: "status-monitor:cycle",
      status: "firing",
      severity: "critical",
      context: { reason: "unknown" },
    });
    expect(db.meta.get("cycle_alert")).toBe("2026-06-25T00:00:00Z");
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:cycle")).toBe("control-plane");
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:cycle"),
    ).toBe(false); // completed atomically with the marker write

    await runCycleAlert(env, config, failing); // sustained outage: no re-page
    expect(bodies).toHaveLength(1);

    await runCycleAlert(env, config, healthy);
    expect(JSON.parse(bodies[1])).toMatchObject({
      alert_type: "monitoring_cycle_failure",
      status: "resolved",
      severity: "info",
      context: {},
    });
    expect(db.meta.has("cycle_alert")).toBe(false);
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:cycle")).toBe(false);
  });

  it("replays the exact pending cycle transition before acting on a fresh edge", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      if (bodies.length === 1) throw new Error("response lost");
      return acceptedRpcResult();
    });
    const env = cycleControlPlaneEnv(db, submit);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    await runCycleAlert(env, config, failing);
    expect(db.meta.has("cycle_alert")).toBe(false); // ambiguous: nothing committed
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:cycle"),
    ).toBe(true);

    // The probe recovered in the meantime — the pending firing must still
    // converge first (same bytes, same event_id), not be dropped for the edge.
    await runCycleAlert(env, config, healthy);
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
    expect(db.meta.get("cycle_alert")).toBe("alerted");
    expect(
      db.meta.has("alert_delivery_pending:v1:firing:status-monitor:cycle"),
    ).toBe(false);

    // Next run sees the converged firing state and resolves normally.
    await runCycleAlert(env, config, healthy);
    expect(JSON.parse(bodies[2])).toMatchObject({ status: "resolved" });
    expect(db.meta.has("cycle_alert")).toBe(false);
  });

  it("does not pin the cycle writer on an undeliverable attempt", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    // Feature off (flag unset → legacy) and no legacy destination: the firing
    // edge is undeliverable, and must NOT claim the incident for legacy.
    const off = {
      ...(cycleControlPlaneEnv(db, submit) as unknown as Record<string, unknown>),
      ALERT_CYCLE_OWNER: undefined,
    } as unknown as Env;
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);

    await runCycleAlert(off, config, failing);
    expect(log).toHaveBeenCalledWith(
      "legacy alert undeliverable (no relay or webhook configured): Monitoring cycle failing",
    );
    expect(submit).not.toHaveBeenCalled();
    // Pinning happens on commit, not attempt — otherwise this row would read
    // "legacy" forever and the later flag flip below would be a no-op, wedging
    // the cycle alert on a writer that can never deliver it.
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:cycle")).toBe(false);

    // Ops flips ALERT_CYCLE_OWNER: the still-open outage must go control-plane.
    await runCycleAlert(cycleControlPlaneEnv(db, submit), config, failing);
    expect(bodies).toHaveLength(1);
    expect(JSON.parse(bodies[0])).toMatchObject({
      alert_type: "monitoring_cycle_failure",
      status: "firing",
    });
    expect(db.meta.get("alert_delivery_owner:v1:status-monitor:cycle")).toBe(
      "control-plane",
    );
    expect(db.meta.get("cycle_alert")).toBe("2026-06-25T00:00:00Z");
  });

  it("keeps a control-plane cycle incident on its writer after rollback", async () => {
    const db = new FakeD1();
    const bodies: string[] = [];
    const submit = vi.fn(async (bodyJson: string) => {
      bodies.push(bodyJson);
      return acceptedRpcResult();
    });
    const opened = cycleControlPlaneEnv(db, submit);
    await runCycleAlert(opened, config, failing);
    expect(bodies).toHaveLength(1);

    // ALERT_CYCLE_OWNER rolled back to legacy with a webhook configured: the
    // open incident must still resolve through the control plane, not fork a
    // second recovery onto the webhook.
    const rolledBack = {
      ...(cycleControlPlaneEnv(db, submit) as unknown as Record<string, unknown>),
      ALERT_CYCLE_OWNER: undefined,
      SLACK_WEBHOOK_URL: "https://hook.test/x",
    } as unknown as Env;
    const posts = stubFetch();

    await runCycleAlert(rolledBack, config, healthy);
    expect(posts).toHaveLength(0);
    expect(JSON.parse(bodies[1])).toMatchObject({ status: "resolved" });
    expect(db.meta.has("alert_delivery_owner:v1:status-monitor:cycle")).toBe(false);
  });

  it("pages once when the cycle starts failing and not again while it stays down", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await runCycleAlert(env, config, failing);
    expect(posts).toHaveLength(1);
    expect(posts[0].text).toContain("Monitoring cycle failing");
    expect(db.meta.get("cycle_alert")).toBeTruthy();

    await runCycleAlert(env, config, failing); // still down → no repeat
    expect(posts).toHaveLength(1);
  });

  it("pages a recovery and clears state when the cycle succeeds again", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await runCycleAlert(env, config, failing); // down (1)
    await runCycleAlert(env, config, healthy); // recovered (2)
    expect(posts).toHaveLength(2);
    expect(posts[1].text).toContain("Monitoring cycle recovered");
    expect(db.meta.has("cycle_alert")).toBe(false);
  });

  it("uses the fixed cycle fingerprint for relay firing and recovery events", async () => {
    const db = new FakeD1();
    const env = envWith(db, undefined, "https://relay.test", "relay-token");
    const events: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        events.push(JSON.parse(String(init.body)));
        return new Response(null, { status: 200 });
      }),
    );

    await runCycleAlert(env, config, failing);
    await runCycleAlert(env, config, healthy);

    expect(events.map((event) => event.fingerprint)).toEqual([
      "status-monitor:cycle",
      "status-monitor:cycle",
    ]);
    expect(events.map((event) => event.status)).toEqual(["firing", "resolved"]);
    expect(events.map((event) => event.severity)).toEqual(["critical", "info"]);
    expect(db.meta.has("cycle_alert")).toBe(false);
  });

  it("keeps cycle alerts on the legacy writer when individual alerts use the control plane", async () => {
    const db = new FakeD1();
    const submit = vi.fn(async () => acceptedRpcResult());
    const env = controlPlaneEnv(db, submit, "control-plane", "https://hook.test/x");
    const posts = stubFetch();

    await runCycleAlert(env, config, failing);
    await runCycleAlert(env, config, healthy);

    expect(submit).not.toHaveBeenCalled();
    expect(posts).toHaveLength(2);
    expect(posts[0].text).toContain("Monitoring cycle failing");
    expect(posts[1].text).toContain("Monitoring cycle recovered");
  });

  it("does not record the cycle as alerted when the POST fails (retries next cycle)", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    let status = 500;
    const posts: Array<{ text: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        posts.push(JSON.parse(String(init.body)));
        return new Response("x", { status });
      }),
    );

    await runCycleAlert(env, config, failing); // POST 500 → not committed
    expect(db.meta.has("cycle_alert")).toBe(false);

    status = 200;
    await runCycleAlert(env, config, failing); // retried → delivered
    expect(posts).toHaveLength(2);
    expect(db.meta.get("cycle_alert")).toBeTruthy();
  });

  it("never pages a recovery for a cycle that was never alerted down", async () => {
    const db = new FakeD1();
    const env = envWith(db, "https://hook.test/x");
    const posts = stubFetch();

    await runCycleAlert(env, config, healthy);
    expect(posts).toHaveLength(0);
  });

  // Cycle-level failures are the most severe class this worker emits; with no
  // destination they must reach deliverAlert's undeliverable report rather
  // than no-op at the door. The edge stays unrecorded, so it retries (and the
  // report repeats) every cycle until a destination exists.
  it("reports an undeliverable cycle alert instead of silently no-oping", async () => {
    const db = new FakeD1();
    const env = envWith(db, undefined);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);

    await runCycleAlert(env, config, failing);
    expect(fetchMock).not.toHaveBeenCalled();
    const undeliverable =
      "legacy alert undeliverable (no relay or webhook configured): Monitoring cycle failing";
    expect(log).toHaveBeenCalledWith(undeliverable);
    expect(db.meta.has("cycle_alert")).toBe(false);

    await runCycleAlert(env, config, failing);
    expect(log.mock.calls.filter(([message]) => message === undeliverable)).toHaveLength(2);
    log.mockRestore();
  });
});
