import { renderDashboard } from "./dashboard";
import {
  acquireCycleLock,
  getSnapshot,
  prune,
  reconcileModels,
  recordResults,
  releaseCycleLock,
  setCycleStatus,
} from "./db";
import { loadConfig, type Env } from "./env";
import { discoverModels } from "./models";
import { probeModel, type ProbeResult } from "./probe";

// Safety-net expiry for the single-cycle lock; well above a healthy cycle but
// short enough that a crashed invocation can't wedge probing for long.
const CYCLE_LOCK_TTL_MS = 15 * 60 * 1000;

/** Runs `worker` over `items` with at most `limit` in flight at once. */
async function mapPool<T, R>(
  items: T[],
  limit: number,
  worker: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      const i = next++;
      if (i >= items.length) return;
      results[i] = await worker(items[i]);
    }
  });
  await Promise.all(runners);
  return results;
}

/** Probes every discovered model once and records the results in D1. */
async function runProbeCycle(env: Env): Promise<void> {
  const config = loadConfig(env);
  const now = () => new Date().toISOString();
  if (!env.PROBER_API_KEY) {
    console.error("PROBER_API_KEY is not set; skipping probe cycle.");
    await setCycleStatus(env.DB, { ok: false, checkedAt: now(), error: "PROBER_API_KEY not set" });
    return;
  }

  // Don't let an overlapping invocation start a second pool against the same
  // key (their combined concurrency would exceed the gateway cap → 429s).
  const lock = await acquireCycleLock(env.DB, Date.now(), CYCLE_LOCK_TTL_MS);
  if (!lock) {
    console.log("previous probe cycle still running; skipping this invocation.");
    return;
  }

  try {
    // Discovering the catalog is itself a probe of the gateway. If it fails, the
    // gateway is down or the key is invalid — record that so the dashboard turns
    // unhealthy instead of serving stale green rows.
    let targets;
    try {
      targets = await discoverModels(config, env.PROBER_API_KEY);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(`model discovery failed: ${message}`);
      await setCycleStatus(env.DB, { ok: false, checkedAt: now(), error: message });
      return;
    }

    const results: ProbeResult[] = await mapPool(targets, config.maxConcurrency, (target) =>
      probeModel(config, env.PROBER_API_KEY, target),
    );
    await recordResults(env.DB, results);
    await reconcileModels(
      env.DB,
      results.map((r) => r.modelId),
    );
    await prune(env.DB, config.retentionDays);
    await setCycleStatus(env.DB, { ok: true, checkedAt: now(), error: null });
    const down = results.filter((r) => !r.ok).length;
    console.log(`probe cycle complete: ${results.length} models, ${down} down`);
  } finally {
    await releaseCycleLock(env.DB, lock);
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export default {
  /** Cron Trigger: probe every model every 5 minutes. */
  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runProbeCycle(env));
  },

  /** HTTP handler: dashboard + JSON status/health. */
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (path === "/api/health") {
      const snap = await getSnapshot(env.DB);
      return json(
        {
          status: snap.cycle.ok ? "ok" : "degraded",
          total: snap.total,
          healthy: snap.healthy,
          unhealthy: snap.unhealthy,
          lastCycleAt: snap.cycle.checkedAt,
          lastCycleError: snap.cycle.error,
        },
        snap.cycle.ok ? 200 : 503,
      );
    }
    if (path === "/api/status") {
      return json(await getSnapshot(env.DB));
    }
    if (path === "/") {
      const snap = await getSnapshot(env.DB);
      return new Response(renderDashboard(snap), {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
    return new Response("Not found", { status: 404 });
  },
};
