import { renderDashboard } from "./dashboard";
import { getSnapshot, prune, recordResults } from "./db";
import { loadConfig, type Env } from "./env";
import { discoverModels } from "./models";
import { probeModel, type ProbeResult } from "./probe";

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
  if (!env.PROBER_API_KEY) {
    console.error("PROBER_API_KEY is not set; skipping probe cycle.");
    return;
  }
  const targets = await discoverModels(config, env.PROBER_API_KEY);
  const results: ProbeResult[] = await mapPool(targets, config.maxConcurrency, (target) =>
    probeModel(config, env.PROBER_API_KEY, target),
  );
  await recordResults(env.DB, results);
  await prune(env.DB, config.retentionDays);
  const down = results.filter((r) => !r.ok).length;
  console.log(`probe cycle complete: ${results.length} models, ${down} down`);
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
      return json({
        status: "ok",
        total: snap.total,
        healthy: snap.healthy,
        unhealthy: snap.unhealthy,
      });
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
