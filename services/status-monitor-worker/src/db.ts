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
 */
export async function reconcileModels(db: D1Database, activeIds: string[]): Promise<void> {
  if (activeIds.length === 0) return; // never wipe everything on an empty cycle
  const placeholders = activeIds.map(() => "?").join(",");
  await db
    .prepare(`DELETE FROM probe_results WHERE model_id NOT IN (${placeholders})`)
    .bind(...activeIds)
    .run();
}

/**
 * Tries to acquire the single-cycle lock, preventing overlapping cron
 * invocations from running probe pools against the same key at once (which
 * would exceed the gateway concurrency cap). Returns true if acquired.
 *
 * The lock auto-expires after `ttlMs` so a crashed invocation can't wedge it.
 */
export async function acquireCycleLock(
  db: D1Database,
  nowMs: number,
  ttlMs: number,
): Promise<boolean> {
  const expiry = String(nowMs + ttlMs);
  // Atomic: insert if absent, or take over only if the existing lock expired.
  const result = await db
    .prepare(
      `INSERT INTO meta (key, value) VALUES ('cycle_lock', ?)
       ON CONFLICT(key) DO UPDATE SET value = ?
       WHERE CAST(meta.value AS INTEGER) < ?`,
    )
    .bind(expiry, expiry, nowMs)
    .run();
  return (result.meta.changes ?? 0) > 0;
}

/** Releases the single-cycle lock. */
export async function releaseCycleLock(db: D1Database): Promise<void> {
  await db.prepare(`DELETE FROM meta WHERE key = 'cycle_lock'`).run();
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

async function getCycleStatus(db: D1Database): Promise<CycleStatus> {
  const result = await db.prepare(`SELECT key, value FROM meta`).all<{ key: string; value: string }>();
  const map = new Map((result.results ?? []).map((r) => [r.key, r.value]));
  return {
    ok: map.get("last_cycle_ok") !== "0", // default ok until a failure is recorded
    checkedAt: map.get("last_cycle_at") || null,
    error: map.get("last_cycle_error") || null,
  };
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
