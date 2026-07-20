import type {
  AlertEventV2,
  AlertReceipt,
  AnalysisJob,
  CodexStatus,
  Incident,
  JobCompletion,
  TrustedEnvironment,
} from "./types";
import { parseAlertEvent, parseCompletion } from "./validation";

export class StoreConflictError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StoreConflictError";
  }
}

export interface RelayStore {
  getReceipt(alertId: string): Promise<AlertReceipt | null>;
  getIncident(incidentId: string): Promise<Incident | null>;
  getActiveIncident(
    environment: TrustedEnvironment,
    fingerprint: string,
  ): Promise<Incident | null>;
  getJob(jobId: string): Promise<AnalysisJob | null>;
  getJobForIncident(incidentId: string): Promise<AnalysisJob | null>;
  createOpening(
    event: AlertEventV2,
    environment: TrustedEnvironment,
    channelId: string,
    incidentId: string,
    jobId: string,
    now: string,
  ): Promise<Incident>;
  activateIncident(incidentId: string, threadTs: string, now: string): Promise<Incident>;
  recordRepeat(incidentId: string, event: AlertEventV2, now: string): Promise<Incident>;
  resolveIncident(
    incidentId: string,
    event: AlertEventV2,
    recoveryMessageId: string,
    now: string,
  ): Promise<Incident>;
  recordOrphanResolution(event: AlertEventV2, now: string): Promise<void>;
  claimParentSync(
    incidentId: string,
    token: string,
    expiresAt: number,
    nowEpoch: number,
  ): Promise<boolean>;
  releaseParentSync(
    incidentId: string,
    token: string,
    renderedVersion: number | null,
    now: string,
  ): Promise<void>;
  markRecoveryPosted(incidentId: string, now: string): Promise<void>;
  setAnalysisRef(jobId: string, analysisRef: string, attempt: number, now: string): Promise<boolean>;
  markDispatched(jobId: string, now: string): Promise<void>;
  retryDispatch(jobId: string, error: string, final: boolean, now: string): Promise<void>;
  beginCompletion(jobId: string, completion: JobCompletion, now: string): Promise<boolean>;
  finishCompletion(
    jobId: string,
    status: "completed" | "failed",
    codexStatus: CodexStatus,
    now: string,
  ): Promise<void>;
  resetCompletion(jobId: string, error: string, now: string): Promise<void>;
}

type Row = Record<string, unknown>;

function activeKey(environment: TrustedEnvironment, fingerprint: string): string {
  return JSON.stringify([environment, fingerprint]);
}

function parseIncident(row: Row): Incident {
  return {
    id: String(row.id),
    environment: String(row.environment) as TrustedEnvironment,
    fingerprint: String(row.fingerprint),
    status: String(row.status) as Incident["status"],
    alert: parseAlertEvent(JSON.parse(String(row.alert_json))),
    resolutionAlert: row.resolution_alert_json
      ? parseAlertEvent(JSON.parse(String(row.resolution_alert_json)))
      : null,
    occurrenceCount: Number(row.occurrence_count),
    firstSeen: String(row.first_seen),
    lastSeen: String(row.last_seen),
    slackChannelId: String(row.slack_channel_id),
    slackThreadTs: row.slack_thread_ts ? String(row.slack_thread_ts) : null,
    codexStatus: String(row.codex_status) as CodexStatus,
    analysisRef: row.analysis_ref ? String(row.analysis_ref) : null,
    parentDirty: Number(row.parent_dirty) === 1,
    parentVersion: Number(row.parent_version),
    recoveryPending: Number(row.recovery_pending) === 1,
    recoveryMessageId: row.recovery_message_id ? String(row.recovery_message_id) : null,
  };
}

function parseJob(row: Row): AnalysisJob {
  return {
    id: String(row.id),
    incidentId: String(row.incident_id),
    status: String(row.status) as AnalysisJob["status"],
    analysisRef: row.analysis_ref ? String(row.analysis_ref) : null,
    attempts: Number(row.attempts),
    lastError: row.last_error ? String(row.last_error) : null,
    completion: row.completion_json
      ? parseCompletion(JSON.parse(String(row.completion_json)))
      : null,
  };
}

function changes(result: D1Result<unknown>): number {
  return Number(result.meta?.changes ?? 0);
}

function isConstraintError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /UNIQUE constraint|constraint failed/i.test(message);
}

export class D1RelayStore implements RelayStore {
  constructor(private readonly db: D1Database) {}

  async getReceipt(alertId: string): Promise<AlertReceipt | null> {
    const row = await this.db
      .prepare(
        "SELECT alert_id, incident_id, action, received_at FROM alert_receipts WHERE alert_id = ?",
      )
      .bind(alertId)
      .first<Row>();
    if (!row) return null;
    return {
      alertId: String(row.alert_id),
      incidentId: row.incident_id ? String(row.incident_id) : null,
      action: String(row.action) as AlertReceipt["action"],
      receivedAt: String(row.received_at),
    };
  }

  async getIncident(incidentId: string): Promise<Incident | null> {
    const row = await this.db
      .prepare("SELECT * FROM alert_incidents WHERE id = ?")
      .bind(incidentId)
      .first<Row>();
    return row ? parseIncident(row) : null;
  }

  async getActiveIncident(
    environment: TrustedEnvironment,
    fingerprint: string,
  ): Promise<Incident | null> {
    const row = await this.db
      .prepare("SELECT * FROM alert_incidents WHERE active_key = ?")
      .bind(activeKey(environment, fingerprint))
      .first<Row>();
    return row ? parseIncident(row) : null;
  }

  async getJob(jobId: string): Promise<AnalysisJob | null> {
    const row = await this.db
      .prepare("SELECT * FROM alert_jobs WHERE id = ?")
      .bind(jobId)
      .first<Row>();
    return row ? parseJob(row) : null;
  }

  async getJobForIncident(incidentId: string): Promise<AnalysisJob | null> {
    const row = await this.db
      .prepare("SELECT * FROM alert_jobs WHERE incident_id = ? ORDER BY created_at LIMIT 1")
      .bind(incidentId)
      .first<Row>();
    return row ? parseJob(row) : null;
  }

  async createOpening(
    event: AlertEventV2,
    environment: TrustedEnvironment,
    channelId: string,
    incidentId: string,
    jobId: string,
    now: string,
  ): Promise<Incident> {
    try {
      await this.db.batch([
        this.db
          .prepare(
            `INSERT INTO alert_incidents (
              id, environment, fingerprint, active_key, status, alert_json,
              resolution_alert_json, occurrence_count, first_seen, last_seen,
              slack_channel_id, slack_thread_ts, codex_status, analysis_ref,
              parent_dirty, parent_version, recovery_pending, recovery_message_id,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'opening', ?, NULL, 1, ?, ?, ?, NULL,
                      'investigating', NULL, 1, 1, 0, NULL, ?, ?)`,
          )
          .bind(
            incidentId,
            environment,
            event.fingerprint,
            activeKey(environment, event.fingerprint),
            JSON.stringify(event),
            event.occurred_at,
            event.occurred_at,
            channelId,
            now,
            now,
          ),
        this.db
          .prepare(
            `INSERT INTO alert_jobs (
              id, incident_id, status, analysis_ref, attempts, last_error,
              completion_json, created_at, updated_at
            ) VALUES (?, ?, 'waiting', NULL, 0, NULL, NULL, ?, ?)`,
          )
          .bind(jobId, incidentId, now, now),
        this.db
          .prepare(
            `INSERT INTO alert_receipts (alert_id, incident_id, action, received_at)
             VALUES (?, ?, 'opened', ?)`,
          )
          .bind(event.alert_id, incidentId, now),
      ]);
    } catch (error) {
      if (isConstraintError(error)) throw new StoreConflictError("alert already accepted");
      throw error;
    }
    const incident = await this.getIncident(incidentId);
    if (!incident) throw new Error("created incident could not be read");
    return incident;
  }

  async activateIncident(incidentId: string, threadTs: string, now: string): Promise<Incident> {
    await this.db.batch([
      this.db
        .prepare(
          `UPDATE alert_incidents
           SET status = 'firing', slack_thread_ts = ?, parent_dirty = 0, updated_at = ?
           WHERE id = ? AND status = 'opening'`,
        )
        .bind(threadTs, now, incidentId),
      this.db
        .prepare(
          `UPDATE alert_jobs SET status = 'queued', updated_at = ?
           WHERE incident_id = ? AND status = 'waiting'`,
        )
        .bind(now, incidentId),
    ]);
    const incident = await this.getIncident(incidentId);
    if (!incident) throw new Error("activated incident could not be read");
    return incident;
  }

  async recordRepeat(incidentId: string, event: AlertEventV2, now: string): Promise<Incident> {
    let results: D1Result<unknown>[];
    try {
      results = await this.db.batch([
        this.db
          .prepare(
            `INSERT INTO alert_receipts (alert_id, incident_id, action, received_at)
             SELECT ?, id, 'repeated', ? FROM alert_incidents
             WHERE id = ? AND status IN ('opening', 'firing')`,
          )
          .bind(event.alert_id, now, incidentId),
        this.db
          .prepare(
            `UPDATE alert_incidents
             SET alert_json = ?, occurrence_count = occurrence_count + 1,
                 last_seen = CASE WHEN last_seen < ? THEN ? ELSE last_seen END,
                 parent_dirty = 1, parent_version = parent_version + 1, updated_at = ?
             WHERE id = ? AND status IN ('opening', 'firing')`,
          )
          .bind(
            JSON.stringify(event),
            event.occurred_at,
            event.occurred_at,
            now,
            incidentId,
          ),
      ]);
    } catch (error) {
      if (isConstraintError(error)) throw new StoreConflictError("alert already accepted");
      throw error;
    }
    if (changes(results[0]) !== 1 || changes(results[1]) !== 1) {
      throw new StoreConflictError("incident is no longer active");
    }
    const incident = await this.getIncident(incidentId);
    if (!incident) throw new Error("repeated incident could not be read");
    return incident;
  }

  async resolveIncident(
    incidentId: string,
    event: AlertEventV2,
    recoveryMessageId: string,
    now: string,
  ): Promise<Incident> {
    let results: D1Result<unknown>[];
    try {
      results = await this.db.batch([
        this.db
          .prepare(
            `INSERT INTO alert_receipts (alert_id, incident_id, action, received_at)
             SELECT ?, id, 'resolved', ? FROM alert_incidents
             WHERE id = ? AND status IN ('opening', 'firing')`,
          )
          .bind(event.alert_id, now, incidentId),
        this.db
          .prepare(
            `UPDATE alert_incidents
             SET active_key = NULL, status = 'resolved', resolution_alert_json = ?,
                 last_seen = CASE WHEN last_seen < ? THEN ? ELSE last_seen END,
                 codex_status = 'resolved', parent_dirty = 1,
                 parent_version = parent_version + 1, recovery_pending = 1,
                 recovery_message_id = ?, updated_at = ?
             WHERE id = ? AND status IN ('opening', 'firing')`,
          )
          .bind(
            JSON.stringify(event),
            event.occurred_at,
            event.occurred_at,
            recoveryMessageId,
            now,
            incidentId,
          ),
      ]);
    } catch (error) {
      if (isConstraintError(error)) throw new StoreConflictError("alert already accepted");
      throw error;
    }
    if (changes(results[0]) !== 1 || changes(results[1]) !== 1) {
      throw new StoreConflictError("incident is no longer active");
    }
    const incident = await this.getIncident(incidentId);
    if (!incident) throw new Error("resolved incident could not be read");
    return incident;
  }

  async recordOrphanResolution(event: AlertEventV2, now: string): Promise<void> {
    try {
      await this.db
        .prepare(
          `INSERT INTO alert_receipts (alert_id, incident_id, action, received_at)
           VALUES (?, NULL, 'orphan_resolution', ?)`,
        )
        .bind(event.alert_id, now)
        .run();
    } catch (error) {
      if (isConstraintError(error)) throw new StoreConflictError("alert already accepted");
      throw error;
    }
  }

  async claimParentSync(
    incidentId: string,
    token: string,
    expiresAt: number,
    nowEpoch: number,
  ): Promise<boolean> {
    const result = await this.db
      .prepare(
        `UPDATE alert_incidents
         SET parent_sync_token = ?, parent_sync_expires_at = ?
         WHERE id = ? AND parent_dirty = 1
           AND (parent_sync_token IS NULL OR parent_sync_expires_at < ?)`,
      )
      .bind(token, expiresAt, incidentId, nowEpoch)
      .run();
    return changes(result) === 1;
  }

  async releaseParentSync(
    incidentId: string,
    token: string,
    renderedVersion: number | null,
    now: string,
  ): Promise<void> {
    await this.db
      .prepare(
        `UPDATE alert_incidents
         SET parent_dirty = CASE WHEN ? IS NOT NULL AND parent_version = ? THEN 0 ELSE 1 END,
             parent_sync_token = NULL, parent_sync_expires_at = NULL, updated_at = ?
         WHERE id = ? AND parent_sync_token = ?`,
      )
      .bind(renderedVersion, renderedVersion, now, incidentId, token)
      .run();
  }

  async markRecoveryPosted(incidentId: string, now: string): Promise<void> {
    await this.db
      .prepare("UPDATE alert_incidents SET recovery_pending = 0, updated_at = ? WHERE id = ?")
      .bind(now, incidentId)
      .run();
  }

  async setAnalysisRef(
    jobId: string,
    analysisRef: string,
    attempt: number,
    now: string,
  ): Promise<boolean> {
    const results = await this.db.batch([
      this.db
        .prepare(
          `UPDATE alert_jobs
           SET status = 'dispatching', analysis_ref = ?, attempts = ?, last_error = NULL,
               updated_at = ?
           WHERE id = ?
             AND (status = 'queued' OR (status = 'dispatching' AND attempts < ?))`,
        )
        .bind(analysisRef, attempt, now, jobId, attempt),
      this.db
        .prepare(
          `UPDATE alert_incidents
           SET analysis_ref = ?, parent_dirty = 1, parent_version = parent_version + 1,
               updated_at = ?
           WHERE changes() = 1
             AND id = (
               SELECT incident_id FROM alert_jobs
               WHERE id = ? AND status = 'dispatching'
                 AND analysis_ref = ? AND attempts = ?
             )`,
        )
        .bind(analysisRef, now, jobId, analysisRef, attempt),
    ]);
    return changes(results[0]) === 1;
  }

  async markDispatched(jobId: string, now: string): Promise<void> {
    await this.db
      .prepare(
        `UPDATE alert_jobs SET status = 'dispatched', last_error = NULL, updated_at = ?
         WHERE id = ? AND status = 'dispatching'`,
      )
      .bind(now, jobId)
      .run();
  }

  async retryDispatch(jobId: string, error: string, final: boolean, now: string): Promise<void> {
    const statements = [
      this.db
        .prepare(
          `UPDATE alert_jobs SET status = ?, last_error = ?, updated_at = ?
           WHERE id = ? AND status IN ('queued', 'dispatching')`,
        )
        .bind(final ? "failed" : "queued", error.slice(0, 2_000), now, jobId),
    ];
    if (final) {
      statements.push(
        this.db
          .prepare(
            `UPDATE alert_incidents
             SET codex_status = CASE WHEN status = 'resolved' THEN 'resolved' ELSE 'unavailable' END,
                 parent_dirty = 1,
                 parent_version = parent_version + 1,
                 updated_at = ?
             WHERE id = (SELECT incident_id FROM alert_jobs WHERE id = ?)`,
          )
          .bind(now, jobId),
      );
    }
    await this.db.batch(statements);
  }

  async beginCompletion(jobId: string, completion: JobCompletion, now: string): Promise<boolean> {
    const result = await this.db
      .prepare(
        `UPDATE alert_jobs
         SET status = 'completing', completion_json = ?, updated_at = ?
         WHERE id = ? AND status IN ('queued', 'dispatching', 'dispatched')`,
      )
      .bind(JSON.stringify(completion), now, jobId)
      .run();
    return changes(result) === 1;
  }

  async finishCompletion(
    jobId: string,
    status: "completed" | "failed",
    codexStatus: CodexStatus,
    now: string,
  ): Promise<void> {
    await this.db.batch([
      this.db
        .prepare(
          `UPDATE alert_jobs SET status = ?, last_error = NULL, updated_at = ?
           WHERE id = ? AND status = 'completing'`,
        )
        .bind(status, now, jobId),
      this.db
        .prepare(
          `UPDATE alert_incidents
           SET codex_status = CASE WHEN status = 'resolved' THEN 'resolved' ELSE ? END,
               parent_dirty = 1, parent_version = parent_version + 1, updated_at = ?
           WHERE id = (SELECT incident_id FROM alert_jobs WHERE id = ?)`,
        )
        .bind(codexStatus, now, jobId),
    ]);
  }

  async resetCompletion(jobId: string, error: string, now: string): Promise<void> {
    await this.db
      .prepare(
        `UPDATE alert_jobs
         SET status = 'dispatched', last_error = ?, completion_json = NULL, updated_at = ?
         WHERE id = ? AND status = 'completing'`,
      )
      .bind(error.slice(0, 2_000), now, jobId)
      .run();
  }
}
