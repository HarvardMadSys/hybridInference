import { renderDashboard } from "./dashboard";
import {
  acquireCycleLock,
  getSnapshot,
  prune,
  reconcileModels,
  recordResults,
  releaseCycleLock,
  renewCycleLock,
  setCycleStatus,
} from "./db";
import { loadConfig, type Env } from "./env";
import { discoverModels } from "./models";
import { probeModel, type ProbeResult } from "./probe";

// Lock lease TTL. A live cycle renews well within this window; only a crashed
// invocation lets it lapse so a successor can take over.
const CYCLE_LOCK_TTL_MS = 10 * 60 * 1000;
// Renew comfortably inside the TTL so a slow-but-live cycle never looks expired.
const CYCLE_LOCK_RENEW_MS = 2 * 60 * 1000;

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

  // Keep extending the lease while this cycle runs, so a slow-but-live cycle is
  // never seen as expired and taken over (which would overlap the pools). The
  // sleep is wakeable so the lock is released the instant the cycle finishes.
  let renewing = true;
  let wake = () => {};
  const heartbeat = (async () => {
    while (renewing) {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, CYCLE_LOCK_RENEW_MS);
        wake = () => {
          clearTimeout(timer);
          resolve();
        };
      });
      if (!renewing) break;
      try {
        await renewCycleLock(env.DB, lock, Date.now(), CYCLE_LOCK_TTL_MS);
      } catch (err) {
        // A transient renew failure must not crash the heartbeat or block lock
        // release; the lease's TTL still bounds takeover.
        console.error("cycle lock renew failed", err);
      }
    }
  })();

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

    // A bad/unverified/over-quota key is rejected for every model at the account
    // level (401/403/429) before routing. Report that as a single cycle failure
    // instead of recording every model as a false outage.
    if (isAccountLevelFailure(results)) {
      console.error("all probes rejected at account level (401/403/429).");
      await setCycleStatus(env.DB, {
        ok: false,
        checkedAt: now(),
        error: "probes rejected account-wide (401/403/429); check PROBER_API_KEY, verification, and quota",
      });
      return;
    }

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
    renewing = false;
    wake(); // wake the pending sleep so the lock releases immediately
    // Release must run even if awaiting the heartbeat throws, or the lock would
    // leak until its lease expires and skip subsequent crons.
    try {
      await heartbeat;
    } finally {
      await releaseCycleLock(env.DB, lock);
    }
  }
}

// Statuses the gateway returns for the whole account before model routing:
// 401 (bad key), 403 (unverified), 429 (quota). Uniform across all probes they
// indicate an account problem, not provider outages.
const ACCOUNT_REJECT_CODES = ["401", "403", "429"];

/** True when every probe failed with the same account-level rejection. */
export function isAccountLevelFailure(results: ProbeResult[]): boolean {
  return (
    results.length > 0 &&
    results.every((r) => !r.ok && ACCOUNT_REJECT_CODES.some((c) => r.error?.includes(c)))
  );
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
