import { runAlerts, runCycleAlert } from "./alerts";
import { renderDashboard } from "./dashboard";
import {
  acquireCycleLock,
  type CycleStatus,
  getSnapshot,
  prune,
  reconcileModels,
  recordResults,
  releaseCycleLock,
  renewCycleLock,
  setCycleStatus,
} from "./db";
import { type Config, loadConfig, type Env } from "./env";
import { discoverModels } from "./models";
import { probeModel, type ProbeResult } from "./probe";
import { hasAlertDestination } from "./oncall";

/**
 * Records the cycle's health and, edge-triggered, pages Slack when the whole
 * cycle fails (gateway unreachable / key rejected account-wide) or recovers.
 * Called at every cycle-status write so a gateway-level outage — which returns
 * before the per-model alerter — is not silent. The Slack/D1 work is contained
 * so it can never break the cycle.
 */
async function finalizeCycle(env: Env, config: Config, status: CycleStatus): Promise<void> {
  await setCycleStatus(env.DB, status);
  try {
    await runCycleAlert(env, config, status);
  } catch (err) {
    console.error("cycle alert failed", err);
  }
}

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
    await finalizeCycle(env, config, { ok: false, checkedAt: now(), error: "PROBER_API_KEY not set" });
    return;
  }

  // Don't let an overlapping invocation start a second pool against the same
  // key (their combined concurrency would exceed the gateway cap → 429s).
  const lock = await acquireCycleLock(env.DB, Date.now(), CYCLE_LOCK_TTL_MS);
  if (!lock) {
    console.log("previous probe cycle still running; skipping this invocation.");
    return;
  }

  // Alerting is opt-in via either the Codex relay or direct Slack fallback.
  // Log once per cycle when neither complete path is configured.
  if (!hasAlertDestination(env)) {
    console.warn(
      "alert delivery disabled; configure the Codex on-call relay or " +
        "set SLACK_WEBHOOK_URL.",
    );
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
      await finalizeCycle(env, config, { ok: false, checkedAt: now(), error: message });
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
      await finalizeCycle(env, config, {
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
    // finalizeCycle records health and, if the cycle was previously failing at
    // the gateway level, pages a recovery notice.
    await finalizeCycle(env, config, { ok: true, checkedAt: now(), error: null });
    // Page Slack for models that crossed the consecutive-failure threshold. A
    // webhook/D1 hiccup here must not fail the cycle or leak the lock, so it is
    // contained — the recorded results above are the source of truth regardless.
    try {
      await runAlerts(env, config, results);
    } catch (err) {
      console.error("alert evaluation failed", err);
    }
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

// Gateway account-level rejection messages (auth/verification/quota), which are
// distinct from upstream/provider failures. Matching the message (not the bare
// status code) avoids misclassifying a provider 401/429 on one model as a
// prober-account problem.
const ACCOUNT_REJECT_PATTERNS = [
  /invalid or expired api key/i, // 401
  /not verified|verify your email/i, // 403
  /quota exceeded|daily cost quota/i, // 429
];

/** True when every probe failed with the same gateway account-level rejection. */
export function isAccountLevelFailure(results: ProbeResult[]): boolean {
  return (
    results.length > 0 &&
    results.every(
      (r) => !r.ok && r.error != null && ACCOUNT_REJECT_PATTERNS.some((p) => p.test(r.error!)),
    )
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export default {
  /** Cron Trigger: probe every model every 20 minutes. */
  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runProbeCycle(env));
  },

  /** HTTP handler: dashboard + JSON status/health. */
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (path === "/api/health") {
      const snap = await getSnapshot(env.DB);
      // Healthy only when the monitor ran cleanly AND no model is down, so an
      // uptime check keyed to this endpoint surfaces detected provider outages.
      const ok = snap.cycle.ok && snap.unhealthy === 0;
      return json(
        {
          status: ok ? "ok" : "degraded",
          total: snap.total,
          healthy: snap.healthy,
          unhealthy: snap.unhealthy,
          cycleOk: snap.cycle.ok,
          lastCycleAt: snap.cycle.checkedAt,
          lastCycleError: snap.cycle.error,
        },
        ok ? 200 : 503,
      );
    }
    if (path === "/api/status") {
      return json(await getSnapshot(env.DB));
    }
    if (path === "/") {
      const snap = await getSnapshot(env.DB);
      let gatewayHost: string | undefined;
      try {
        gatewayHost = new URL(loadConfig(env).gatewayBaseUrl).host;
      } catch {
        gatewayHost = undefined;
      }
      return new Response(renderDashboard(snap, gatewayHost), {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
    return new Response("Not found", { status: 404 });
  },
};
