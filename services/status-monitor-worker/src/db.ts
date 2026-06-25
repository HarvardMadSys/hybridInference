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

/**
 * Returns the subset of `modelIds` whose most recent `threshold` probes were
 * *all* failures — i.e. models that have failed `threshold` consecutive cycles.
 *
 * Reads at most `threshold` rows per model via the `(model_id, id DESC)` index,
 * so the cost is `threshold × modelIds.length` rows regardless of retention. A
 * model with fewer than `threshold` recorded probes is never reported (not yet
 * enough history to confirm a sustained outage). Call after the current cycle's
 * results are recorded so the newest row reflects this cycle.
 */
export async function modelsFailingStreak(
  db: D1Database,
  modelIds: string[],
  threshold: number,
): Promise<Set<string>> {
  const failing = new Set<string>();
  if (modelIds.length === 0 || threshold < 1) return failing;
  const stmt = db.prepare(
    `SELECT ok FROM probe_results WHERE model_id = ? ORDER BY id DESC LIMIT ?`,
  );
  const batched = await db.batch<{ ok: number }>(modelIds.map((id) => stmt.bind(id, threshold)));
  for (let i = 0; i < modelIds.length; i++) {
    const rows = batched[i].results ?? [];
    if (rows.length >= threshold && rows.every((r) => r.ok === 0)) {
      failing.add(modelIds[i]);
    }
  }
  return failing;
}

const ALERT_STATE_KEY = "alert_state";

/**
 * Reads the per-model down-alert state: a map of model id → ISO time it was last
 * alerted as down. A model's presence means an alert has already fired for its
 * current outage, so the next cron doesn't re-page. Returns `{}` when unset or
 * corrupt (a corrupt value simply re-arms alerting rather than wedging it).
 */
export async function readAlertState(db: D1Database): Promise<Record<string, string>> {
  const row = await db
    .prepare(`SELECT value FROM meta WHERE key = ?`)
    .bind(ALERT_STATE_KEY)
    .first<{ value: string }>();
  if (row?.value != null) {
    try {
      const parsed = JSON.parse(row.value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        const out: Record<string, string> = {};
        for (const [k, v] of Object.entries(parsed)) {
          if (typeof v === "string") out[k] = v;
        }
        return out;
      }
    } catch {
      // Corrupt value: fall through to an empty (re-armed) state.
    }
  }
  return {};
}

/** Persists the per-model down-alert state. */
export async function writeAlertState(db: D1Database, state: Record<string, string>): Promise<void> {
  await db
    .prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`)
    .bind(ALERT_STATE_KEY, JSON.stringify(state))
    .run();
}

const CYCLE_ALERT_KEY = "cycle_alert";

/**
 * Reads the cycle-level (gateway-down) alert marker: the ISO time we last paged
 * that the whole probe cycle is failing, or `null` if no such alert is open. Its
 * presence is what makes the cycle alert edge-triggered — paged once on the
 * transition to unhealthy, not every failing cron.
 */
export async function readCycleAlertState(db: D1Database): Promise<string | null> {
  const row = await db
    .prepare(`SELECT value FROM meta WHERE key = ?`)
    .bind(CYCLE_ALERT_KEY)
    .first<{ value: string }>();
  return row?.value || null;
}

/** Sets (non-empty `value`) or clears (`null`) the cycle-level alert marker. */
export async function writeCycleAlertState(db: D1Database, value: string | null): Promise<void> {
  if (value) {
    await db
      .prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`)
      .bind(CYCLE_ALERT_KEY, value)
      .run();
  } else {
    await db.prepare(`DELETE FROM meta WHERE key = ?`).bind(CYCLE_ALERT_KEY).run();
  }
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
  // Persist the active model list in a single meta row so getSnapshot can read it
  // with an O(1) keyed lookup. Deriving it from probe_results (e.g. SELECT
  // DISTINCT model_id) instead scans the whole covering index, which D1 bills as
  // rows_read proportional to retained history (~models × probes/day ×
  // RETENTION_DAYS) — reviving the full-scan cost this module caps history reads
  // to avoid. Written here because a successful cycle's active set is exactly
  // what the dashboard should show.
  const sortedIds = [...activeIds].sort((a, b) => a.localeCompare(b));
  const setModelIds = db
    .prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES ('model_ids', ?)`)
    .bind(JSON.stringify(sortedIds));

  if (activeIds.length === 0) {
    await db.batch([db.prepare(`DELETE FROM probe_results`), setModelIds]);
    return;
  }
  const placeholders = activeIds.map(() => "?").join(",");
  await db.batch([
    db.prepare(`DELETE FROM probe_results WHERE model_id NOT IN (${placeholders})`).bind(...activeIds),
    setModelIds,
  ]);
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
 * Resolves the active model list for a snapshot.
 *
 * The fast path is the single `meta.model_ids` row that {@link reconcileModels}
 * writes each cycle, read with an O(1) keyed lookup so per-request cost is bound
 * to model count, not table size. (A `SELECT DISTINCT model_id` instead scans the
 * whole covering index, which D1 bills as rows_read ~ models × probes/day ×
 * RETENTION_DAYS.)
 *
 * A *written* list — including an empty `[]` for a legitimately empty catalog —
 * is authoritative. Only when the key is **absent or corrupt** (first deploy
 * against an existing DB, or a stretch of only-failed cycles that returned before
 * reconcileModels could write it) do we fall back to a one-off DISTINCT scan, so
 * existing history still renders instead of a blank dashboard. That scan is
 * bounded by table size but transient: the next successful cycle writes the keyed
 * list and reverts reads to O(1).
 */
async function readModelIds(db: D1Database): Promise<string[]> {
  const row = await db
    .prepare(`SELECT value FROM meta WHERE key = 'model_ids'`)
    .first<{ value: string }>();
  if (row?.value != null) {
    try {
      const parsed = JSON.parse(row.value);
      if (Array.isArray(parsed)) {
        return parsed.filter((id): id is string => typeof id === "string");
      }
    } catch {
      // Corrupt value: fall through to the backfill scan.
    }
  }
  const scan = await db
    .prepare(`SELECT DISTINCT model_id FROM probe_results ORDER BY model_id ASC`)
    .all<{ model_id: string }>();
  return (scan.results ?? []).map((r) => r.model_id);
}

/**
 * Builds the dashboard snapshot: the most recent {@link HISTORY_LIMIT} rows per
 * model, with the latest result, a sparkline window, and an uptime ratio.
 */
export async function getSnapshot(db: D1Database): Promise<Snapshot> {
  const modelIds = await readModelIds(db);

  const byModel = new Map<string, ProbeRow[]>();
  if (modelIds.length > 0) {
    // Fetch each model's newest HISTORY_LIMIT rows via the (model_id, id DESC)
    // index — at most HISTORY_LIMIT rows read per model regardless of retention.
    const stmt = db.prepare(
      `SELECT model_id, ok, latency_ms, ttft_ms, completion_tokens, throughput_tps, error, checked_at
       FROM probe_results
       WHERE model_id = ?
       ORDER BY id DESC
       LIMIT ?`,
    );
    const batched = await db.batch<RawRow>(modelIds.map((id) => stmt.bind(id, HISTORY_LIMIT)));
    for (let i = 0; i < modelIds.length; i++) {
      const rows = batched[i].results ?? [];
      // A model can be listed but have no rows — pruned or reconciled away
      // between the list read and this batch, or before its first probe landed.
      // Skip it so `latest` (history[last]) is never undefined downstream.
      if (rows.length === 0) continue;
      // Rows come back newest-first; reverse to oldest→newest so `latest` is the
      // last element and the sparkline tail is the most recent window.
      byModel.set(modelIds[i], rows.map(toRow).reverse());
    }
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
