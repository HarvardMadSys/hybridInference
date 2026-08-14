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
export async function recordResults(
  db: D1Database,
  results: ProbeResult[],
  targetEnvironment: string,
): Promise<void> {
  if (results.length === 0) return;
  const stmt = db.prepare(
    `INSERT INTO probe_results
       (model_id, ok, latency_ms, ttft_ms, completion_tokens, throughput_tps, error,
        checked_at, target_environment)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
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
        targetEnvironment,
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
  targetEnvironment: string,
): Promise<Set<string>> {
  const failing = new Set<string>();
  if (modelIds.length === 0 || threshold < 1) return failing;
  // Scoped to one deployment: this decides whether to page, and a streak that
  // reached back across the 2026-08-11 cutover would count staging failures
  // toward a production outage. The window is short, so the mixing only shows
  // up for a model absent from the catalog long enough for its newest rows to
  // predate the switch — which is exactly when nobody would think to check.
  const stmt = db.prepare(
    `SELECT ok FROM probe_results
      WHERE model_id = ? AND target_environment = ?
      ORDER BY id DESC LIMIT ?`,
  );
  const batched = await db.batch<{ ok: number }>(
    modelIds.map((id) => stmt.bind(id, targetEnvironment, threshold)),
  );
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
 * Reads the per-model down-alert state: a map of model id → incident fingerprint.
 * A model's presence means an alert has already fired for its current outage, so
 * the next cron doesn't re-page. Legacy ISO timestamp values remain readable and
 * are normalized by the caller. Returns `{}` when unset or corrupt (a corrupt
 * value simply re-arms alerting rather than wedging it).
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
        // Null prototype: model ids are attacker-adjacent map keys, and a
        // plain object would drop "__proto__" and inherit "constructor"
        // (same hardening as the dashboard's payload map).
        const out: Record<string, string> = Object.create(null);
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

/** Builds the alert-state write used in the atomic delivery completion batch. */
export function prepareAlertStateWrite(
  db: D1Database,
  state: Record<string, string>,
): D1PreparedStatement {
  return db
    .prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`)
    .bind(ALERT_STATE_KEY, JSON.stringify(state));
}

/** Persists the per-model down-alert state. */
export async function writeAlertState(db: D1Database, state: Record<string, string>): Promise<void> {
  await prepareAlertStateWrite(db, state).run();
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

/**
 * Builds the cycle-marker write (set on non-empty `value`, clear on `null`) for
 * use inside an atomic batch alongside pending-transition completion.
 */
export function prepareCycleAlertStateWrite(
  db: D1Database,
  value: string | null,
): D1PreparedStatement {
  return value
    ? db
        .prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`)
        .bind(CYCLE_ALERT_KEY, value)
    : db.prepare(`DELETE FROM meta WHERE key = ?`).bind(CYCLE_ALERT_KEY);
}

/** Sets (non-empty `value`) or clears (`null`) the cycle-level alert marker. */
export async function writeCycleAlertState(db: D1Database, value: string | null): Promise<void> {
  await prepareCycleAlertStateWrite(db, value).run();
}

/** Deletes probe rows older than `retentionDays`. */
export async function prune(db: D1Database, retentionDays: number): Promise<void> {
  const cutoff = new Date(Date.now() - retentionDays * 86_400_000).toISOString();
  await db.prepare(`DELETE FROM probe_results WHERE checked_at < ?`).bind(cutoff).run();
}

const MODEL_IDS_KEY = "model_ids";
const MODEL_SWEEP_KEY = "model_ids_swept_at";

/**
 * How long the full `NOT IN` sweep may be deferred. One sweep/day against a
 * 20-minute cron is 1 scan per 72 cycles, which keeps the backstop's cost in the
 * noise while still bounding how long an unreferenced row can occupy storage.
 */
export const MODEL_SWEEP_INTERVAL_MS = 86_400_000;

/** Reads the previous active set and last sweep time in one keyed statement. */
async function readReconcileState(
  db: D1Database,
): Promise<{ previous: string[] | null; rawPrevious: string | null; sweptAtMs: number | null }> {
  const rows = await db
    .prepare(`SELECT key, value FROM meta WHERE key IN (?,?)`)
    .bind(MODEL_IDS_KEY, MODEL_SWEEP_KEY)
    .all<{ key: string; value: string | null }>();
  const byKey = new Map((rows.results ?? []).map((r) => [r.key, r.value]));

  let previous: string[] | null = null;
  const rawPrevious = byKey.get(MODEL_IDS_KEY) ?? null;
  if (rawPrevious != null) {
    try {
      const parsed = JSON.parse(rawPrevious);
      if (Array.isArray(parsed) && parsed.every((id) => typeof id === "string")) {
        previous = parsed;
      }
    } catch {
      // Corrupt value: leave `previous` null so the caller does a full sweep.
    }
  }
  const sweptAt = Number(byKey.get(MODEL_SWEEP_KEY));
  return { previous, rawPrevious, sweptAtMs: Number.isFinite(sweptAt) ? sweptAt : null };
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
 *
 * ## Why this diffs instead of scanning
 *
 * The obvious statement — `DELETE ... WHERE model_id NOT IN (active ids)` — is
 * unindexable: SQLite cannot satisfy a negated equality set from
 * `idx_probe_results_model_id`, so it full-scans `probe_results` and D1 bills
 * rows_read ~ the entire retained history (~models × probes/day ×
 * `RETENTION_DAYS`, which at `RETENTION_DAYS=1024` never stops growing). Running
 * that every cycle to normally delete *nothing* was the single largest rows_read
 * source in this Worker.
 *
 * So the common cases avoid it entirely. `meta.model_ids` already records the
 * previous cycle's active set, so diffing it against `activeIds` names the
 * departed models outright: an unchanged catalog issues no `probe_results`
 * statement at all, and a shrunken one deletes by `model_id IN (departed)`,
 * which *does* seek the index and reads only what it removes.
 *
 * The diff has one blind spot: a cycle that dies between `recordResults` and
 * this function leaves rows whose `model_id` was never stored, so no later diff
 * can name them. {@link MODEL_SWEEP_INTERVAL_MS} bounds that leak by falling
 * back to the full sweep periodically — and immediately when the stored set is
 * missing or corrupt (first deploy against an existing database), where there is
 * no previous set to diff against.
 */
export async function reconcileModels(
  db: D1Database,
  activeIds: string[],
  nowMs: number,
  targetEnvironment: string,
): Promise<void> {
  // Persist the active model list in a single meta row so getSnapshot can read it
  // with an O(1) keyed lookup. Deriving it from probe_results (e.g. SELECT
  // DISTINCT model_id) instead scans the whole covering index, at the same
  // rows_read cost described above. Written here because a successful cycle's
  // active set is exactly what the dashboard should show.
  const sortedIds = [...new Set(activeIds)].sort((a, b) => a.localeCompare(b));
  const serialized = JSON.stringify(sortedIds);
  const setModelIds = db
    .prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`)
    .bind(MODEL_IDS_KEY, serialized);
  const markSwept = db
    .prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`)
    .bind(MODEL_SWEEP_KEY, String(nowMs));

  if (sortedIds.length === 0) {
    // Scoped, not unfiltered: an empty catalog says nothing about a deployment
    // this instance does not probe, and the retained pre-cutover history sits in
    // the same table.
    await db.batch([
      db
        .prepare(`DELETE FROM probe_results WHERE target_environment = ?`)
        .bind(targetEnvironment),
      setModelIds,
      markSwept,
    ]);
    return;
  }

  const { previous, rawPrevious, sweptAtMs } = await readReconcileState(db);
  const sweepDue = sweptAtMs === null || nowMs - sweptAtMs >= MODEL_SWEEP_INTERVAL_MS;

  if (previous === null || sweepDue) {
    const placeholders = sortedIds.map(() => "?").join(",");
    await db.batch([
      db
        .prepare(
          `DELETE FROM probe_results
            WHERE target_environment = ? AND model_id NOT IN (${placeholders})`,
        )
        .bind(targetEnvironment, ...sortedIds),
      setModelIds,
      markSwept,
    ]);
    return;
  }

  const active = new Set(sortedIds);
  const departed = previous.filter((id) => !active.has(id));
  const statements: D1PreparedStatement[] = [];
  if (departed.length > 0) {
    const placeholders = departed.map(() => "?").join(",");
    statements.push(
      db
        .prepare(
          `DELETE FROM probe_results
            WHERE target_environment = ? AND model_id IN (${placeholders})`,
        )
        .bind(targetEnvironment, ...departed),
    );
  }
  // Exact string comparison, so any drift in the stored representation (legacy
  // ordering, a duplicate id) is rewritten once and then stops costing anything.
  if (rawPrevious !== serialized) {
    statements.push(setModelIds);
  }
  // Steady state: catalog unchanged, nothing departed — no statement to run.
  if (statements.length === 0) return;
  await db.batch(statements);
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
export async function setCycleStatus(
  db: D1Database,
  status: CycleStatus,
  targetEnvironment: string,
): Promise<void> {
  const stmt = db.prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`);
  await db.batch([
    stmt.bind("last_cycle_ok", status.ok ? "1" : "0"),
    stmt.bind("last_cycle_at", status.checkedAt ?? ""),
    stmt.bind("last_cycle_error", status.error ?? ""),
    // Stamped so a reader can tell whether this result describes the deployment
    // it is asking about. These three rows are a single latest-value slot, not
    // history, so without it a repointed Worker keeps serving the previous
    // gateway's verdict — a green `/api/health` for a deployment it has not yet
    // probed once.
    stmt.bind("last_cycle_target_environment", targetEnvironment),
  ]);
}

// A cycle older than this (≈3 missed 20-minute crons) is treated as stale, so a
// stopped/undeployed cron or a never-run monitor doesn't show stale green.
const CYCLE_FRESHNESS_MS = 60 * 60 * 1000;

async function getCycleStatus(
  db: D1Database,
  targetEnvironment: string,
): Promise<CycleStatus> {
  const result = await db.prepare(`SELECT key, value FROM meta`).all<{ key: string; value: string }>();
  const map = new Map((result.results ?? []).map((r) => [r.key, r.value]));
  const checkedAt = map.get("last_cycle_at") || null;
  if (!checkedAt) {
    return { ok: false, checkedAt: null, error: "no probe cycle has run yet" };
  }
  // A row left by the gateway this Worker used to probe says nothing about the
  // one it probes now. Absent means it predates the stamp, which can only be
  // the deployment before a repoint — so treat it the same way.
  if (map.get("last_cycle_target_environment") !== targetEnvironment) {
    return {
      ok: false,
      checkedAt: null,
      error: "no probe cycle has run yet for this deployment",
    };
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
async function readModelIds(
  db: D1Database,
  targetEnvironment: string,
): Promise<string[]> {
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
  // Scoped like every other read of this table: the retained pre-cutover rows
  // name models of a deployment this Worker no longer probes, and listing them
  // would put permanently blank cards on the dashboard.
  const scan = await db
    .prepare(
      `SELECT DISTINCT model_id FROM probe_results
        WHERE target_environment = ?
        ORDER BY model_id ASC`,
    )
    .bind(targetEnvironment)
    .all<{ model_id: string }>();
  return (scan.results ?? []).map((r) => r.model_id);
}

/**
 * Builds the dashboard snapshot: the most recent {@link HISTORY_LIMIT} rows per
 * model, with the latest result, a sparkline window, and an uptime ratio.
 */
export async function getSnapshot(
  db: D1Database,
  targetEnvironment: string,
): Promise<Snapshot> {
  const modelIds = await readModelIds(db, targetEnvironment);

  const byModel = new Map<string, ProbeRow[]>();
  if (modelIds.length > 0) {
    // Fetch each model's newest HISTORY_LIMIT rows via the
    // (model_id, target_environment, id DESC) index — at most HISTORY_LIMIT rows
    // read per model regardless of retention. Scoping to one deployment is what
    // stops the retained pre-cutover history, which outnumbers the current
    // deployment's rows many times over, from dominating every chart and uptime
    // ratio on a page that presents itself as production.
    const stmt = db.prepare(
      `SELECT model_id, ok, latency_ms, ttft_ms, completion_tokens, throughput_tps, error, checked_at
       FROM probe_results
       WHERE model_id = ? AND target_environment = ?
       ORDER BY id DESC
       LIMIT ?`,
    );
    const batched = await db.batch<RawRow>(
      modelIds.map((id) => stmt.bind(id, targetEnvironment, HISTORY_LIMIT)),
    );
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
  const cycle = await getCycleStatus(db, targetEnvironment);
  return { models, total: models.length, healthy, unhealthy: models.length - healthy, cycle };
}
