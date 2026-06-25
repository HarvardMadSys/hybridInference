import { afterEach, describe, expect, it, vi } from "vitest";

import {
  decideAlerts,
  deriveEnvironment,
  escapeSlackText,
  formatModelDownAlert,
  formatModelRecoveredAlert,
  postSlack,
  runAlerts,
} from "../src/alerts";
import type { Config, Env } from "../src/env";
import type { ProbeResult } from "../src/probe";

const config = {
  gatewayBaseUrl: "https://staging.freeinference.org",
  probePrompt: "hi",
  probeMaxTokens: 32,
  maxConcurrency: 3,
  probeHeader: "synthetic",
  retentionDays: 7,
  probeDeadlineMs: 1000,
  alertFailureThreshold: 2,
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
  afterEach(() => vi.unstubAllGlobals());

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
    throw new Error(`unhandled first(): ${this.sql}`);
  }
  async run(): Promise<{ meta: { changes: number } }> {
    if (/INSERT OR REPLACE INTO meta/.test(this.sql)) {
      this.db.meta.set(this.args[0] as string, this.args[1] as string);
      return { meta: { changes: 1 } };
    }
    throw new Error(`unhandled run(): ${this.sql}`);
  }
  async all<T>(): Promise<{ results: T[] }> {
    if (/SELECT ok FROM probe_results WHERE model_id = \? ORDER BY id DESC LIMIT \?/.test(this.sql)) {
      const modelId = this.args[0] as string;
      const limit = this.args[1] as number;
      const rows = this.db.probe
        .filter((r) => r.model_id === modelId)
        .sort((a, b) => b.id - a.id)
        .slice(0, limit)
        .map((r) => ({ ok: r.ok }));
      return { results: rows as T[] };
    }
    throw new Error(`unhandled all(): ${this.sql}`);
  }
}

class FakeD1 {
  probe: Array<{ id: number; model_id: string; ok: number }> = [];
  meta = new Map<string, string>();
  private seq = 0;
  prepare(sql: string): FakeStmt {
    return new FakeStmt(this, sql);
  }
  async batch<T>(stmts: FakeStmt[]): Promise<Array<{ results: T[] }>> {
    return Promise.all(stmts.map((s) => s.all<T>()));
  }
  record(modelId: string, ok: boolean): void {
    this.probe.push({ id: ++this.seq, model_id: modelId, ok: ok ? 1 : 0 });
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

function cfg(threshold: number): Config {
  return { ...config, alertFailureThreshold: threshold };
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
  afterEach(() => vi.unstubAllGlobals());

  function envWith(db: FakeD1, webhook: string | undefined): Env {
    return { SLACK_WEBHOOK_URL: webhook, DB: db as unknown as D1Database } as unknown as Env;
  }

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

  it("does nothing when no webhook is configured", async () => {
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
});
