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
    expect(first.nextState.a).toBe("2026-06-25T00:00:00Z");

    // Already alerted — a sustained outage must not page again.
    const second = decideAlerts([result("a", false)], new Set(["a"]), first.nextState);
    expect(second.down).toEqual([]);
    expect(second.recovered).toEqual([]);
  });

  it("does not alert a model that is failing but below the threshold", () => {
    // Failing this cycle but not in the >=threshold set yet.
    const d = decideAlerts([result("a", false)], new Set(), {});
    expect(d.down).toEqual([]);
    expect(d.nextState).toEqual({});
  });

  it("emits a recovery alert and clears state when a down model succeeds", () => {
    const d = decideAlerts([result("a", true)], new Set(), { a: "2026-06-25T00:00:00Z" });
    expect(d.recovered.map((r) => r.modelId)).toEqual(["a"]);
    expect(d.nextState.a).toBeUndefined();
  });

  it("drops state for models no longer probed this cycle", () => {
    const d = decideAlerts([result("a", true)], new Set(), { gone: "2026-06-25T00:00:00Z" });
    expect(d.nextState.gone).toBeUndefined();
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

/** Records one cycle's results into the fake DB and evaluates alerts. */
async function cycle(db: FakeD1, env: Env, perModel: Record<string, boolean>): Promise<void> {
  const results = Object.entries(perModel).map(([id, ok]) => result(id, ok));
  for (const r of results) db.record(r.modelId, r.ok);
  await runAlerts(env, config, results);
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

  it("does nothing when no webhook is configured", async () => {
    const db = new FakeD1();
    const env = envWith(db, undefined);
    const posts = stubFetch();

    await cycle(db, env, { a: false });
    await cycle(db, env, { a: false });
    expect(posts).toHaveLength(0);
    expect(db.meta.has("alert_state")).toBe(false);
  });
});
