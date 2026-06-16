import type { ProbeResult } from "./probe";

const HISTORY_LIMIT = 100;
const SPARK_LIMIT = 30;

/** A single stored probe row (camelCased). */
export interface ProbeRow {
  ok: boolean;
  checkedAt: string;
  latencyMs: number | null;
  ttftMs: number | null;
  completionTokens: number | null;
  throughputTps: number | null;
  error: string | null;
}

/** Per-model status: latest result plus recent history and uptime. */
export interface ModelStatus {
  modelId: string;
  latest: ProbeRow;
  history: ProbeRow[];
  spark: ProbeRow[];
  uptimeRatio: number;
}

/** Health of the most recent cron cycle. */
export interface CycleStatus {
  ok: boolean;
  checkedAt: string | null;
  error: string | null;
}

/** Aggregated dashboard snapshot. */
export interface Snapshot {
  models: ModelStatus[];
  total: number;
  healthy: number;
  unhealthy: number;
  cycle: CycleStatus;
}

/** Inserts the results of one probe cycle. */
export async function recordResults(db: D1Database, results: ProbeResult[]): Promise<void> {
  if (results.length === 0) return;
  const stmt = db.prepare(
    `INSERT INTO probe_results
       (model_id, ok, latency_ms, ttft_ms, completion_tokens, throughput_tps, error, checked_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  );
  await db.batch(
    results.map((r) =>
      stmt.bind(
        r.modelId,
        r.ok ? 1 : 0,
        r.latencyMs,
        r.ttftMs,
        r.completionTokens,
        r.throughputTps,
        r.error,
        r.checkedAt,
      ),
    ),
  );
}

/** Deletes probe rows older than `retentionDays`. */
export async function prune(db: D1Database, retentionDays: number): Promise<void> {
  const cutoff = new Date(Date.now() - retentionDays * 86_400_000).toISOString();
  await db.prepare(`DELETE FROM probe_results WHERE checked_at < ?`).bind(cutoff).run();
}

/**
 * Drops probe rows for models no longer in the active set, so models removed,
 * disabled, or hidden by a runtime visibility change leave the dashboard
 * promptly instead of lingering for `RETENTION_DAYS`.
 *
 * Only call this after a *successful* cycle: an empty `activeIds` then means the
 * authenticated catalog is legitimately empty, so all rows are cleared. (A
 * failed discovery is handled by the caller before reaching here, so it never
 * wipes the dashboard during an outage.)
 */
export async function reconcileModels(db: D1Database, activeIds: string[]): Promise<void> {
  if (activeIds.length === 0) {
    await db.prepare(`DELETE FROM probe_results`).run();
    return;
  }
  const placeholders = activeIds.map(() => "?").join(",");
  await db
    .prepare(`DELETE FROM probe_results WHERE model_id NOT IN (${placeholders})`)
    .bind(...activeIds)
    .run();
}

/**
 * Tries to acquire the single-cycle lock, preventing overlapping cron
 * invocations from running probe pools against the same key at once (which
 * would exceed the gateway concurrency cap).
 *
 * The lock value is `"{expiryMs}:{token}"`: the expiry lets a later invocation
 * take over a crashed cycle after `ttlMs`, while a live cycle keeps extending it
 * via {@link renewCycleLock}. The unique token identifies this cycle so renew
 * and {@link releaseCycleLock} only ever touch the lock this cycle owns.
 *
 * @returns The owned token to pass to renew/release, or `null` if another cycle
 *   holds an unexpired lock.
 */
export async function acquireCycleLock(
  db: D1Database,
  nowMs: number,
  ttlMs: number,
): Promise<string | null> {
  const token = crypto.randomUUID();
  const value = `${nowMs + ttlMs}:${token}`;
  // Atomic: insert if absent, or take over only if the existing lock expired.
  // CAST stops at the first non-digit, so it compares the expiry prefix.
  const result = await db
    .prepare(
      `INSERT INTO meta (key, value) VALUES ('cycle_lock', ?)
       ON CONFLICT(key) DO UPDATE SET value = ?
       WHERE CAST(meta.value AS INTEGER) < ?`,
    )
    .bind(value, value, nowMs)
    .run();
  return (result.meta.changes ?? 0) > 0 ? token : null;
}

const LOCK_TOKEN_SQL = `substr(value, instr(value, ':') + 1)`;

/** Extends the lock's expiry, but only while this cycle still owns it. */
export async function renewCycleLock(
  db: D1Database,
  token: string,
  nowMs: number,
  ttlMs: number,
): Promise<void> {
  await db
    .prepare(
      `UPDATE meta SET value = ? WHERE key = 'cycle_lock' AND ${LOCK_TOKEN_SQL} = ?`,
    )
    .bind(`${nowMs + ttlMs}:${token}`, token)
    .run();
}

/** Releases the single-cycle lock, but only if this cycle still owns it. */
export async function releaseCycleLock(db: D1Database, token: string): Promise<void> {
  await db
    .prepare(`DELETE FROM meta WHERE key = 'cycle_lock' AND ${LOCK_TOKEN_SQL} = ?`)
    .bind(token)
    .run();
}

/** Records whether the most recent cron cycle succeeded. */
export async function setCycleStatus(db: D1Database, status: CycleStatus): Promise<void> {
  const stmt = db.prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`);
  await db.batch([
    stmt.bind("last_cycle_ok", status.ok ? "1" : "0"),
    stmt.bind("last_cycle_at", status.checkedAt ?? ""),
    stmt.bind("last_cycle_error", status.error ?? ""),
  ]);
}

// A cycle older than this (≈3 missed 20-minute crons) is treated as stale, so a
// stopped/undeployed cron or a never-run monitor doesn't show stale green.
const CYCLE_FRESHNESS_MS = 60 * 60 * 1000;

async function getCycleStatus(db: D1Database): Promise<CycleStatus> {
  const result = await db.prepare(`SELECT key, value FROM meta`).all<{ key: string; value: string }>();
  const map = new Map((result.results ?? []).map((r) => [r.key, r.value]));
  const checkedAt = map.get("last_cycle_at") || null;
  if (!checkedAt) {
    return { ok: false, checkedAt: null, error: "no probe cycle has run yet" };
  }
  const ageMs = Date.now() - Date.parse(checkedAt);
  if (Number.isFinite(ageMs) && ageMs > CYCLE_FRESHNESS_MS) {
    return { ok: false, checkedAt, error: `probe cycle stale (last run ${checkedAt})` };
  }
  const ok = map.get("last_cycle_ok") === "1";
  return { ok, checkedAt, error: ok ? null : map.get("last_cycle_error") || null };
}

interface RawRow {
  model_id: string;
  ok: number;
  latency_ms: number | null;
  ttft_ms: number | null;
  completion_tokens: number | null;
  throughput_tps: number | null;
  error: string | null;
  checked_at: string;
}

function toRow(r: RawRow): ProbeRow {
  return {
    ok: r.ok === 1,
    checkedAt: r.checked_at,
    latencyMs: r.latency_ms,
    ttftMs: r.ttft_ms,
    completionTokens: r.completion_tokens,
    throughputTps: r.throughput_tps,
    error: r.error,
  };
}

/**
 * Builds the dashboard snapshot: the most recent {@link HISTORY_LIMIT} rows per
 * model, with the latest result, a sparkline window, and an uptime ratio.
 */
export async function getSnapshot(db: D1Database): Promise<Snapshot> {
  // Window function keeps the newest HISTORY_LIMIT rows per model.
  const result = await db
    .prepare(
      `SELECT model_id, ok, latency_ms, ttft_ms, completion_tokens, throughput_tps, error, checked_at
       FROM (
         SELECT *, ROW_NUMBER() OVER (PARTITION BY model_id ORDER BY id DESC) AS rn
         FROM probe_results
       )
       WHERE rn <= ?
       ORDER BY model_id ASC, id ASC`,
    )
    .bind(HISTORY_LIMIT)
    .all<RawRow>();

  const byModel = new Map<string, ProbeRow[]>();
  for (const raw of result.results ?? []) {
    const list = byModel.get(raw.model_id) ?? [];
    list.push(toRow(raw));
    byModel.set(raw.model_id, list);
  }

  const models: ModelStatus[] = [];
  for (const [modelId, history] of [...byModel.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    const okCount = history.filter((h) => h.ok).length;
    models.push({
      modelId,
      latest: history[history.length - 1],
      history,
      spark: history.slice(-SPARK_LIMIT),
      uptimeRatio: history.length ? Math.round((okCount / history.length) * 10000) / 10000 : 0,
    });
  }

  const healthy = models.filter((m) => m.latest.ok).length;
  const cycle = await getCycleStatus(db);
  return { models, total: models.length, healthy, unhealthy: models.length - healthy, cycle };
}
