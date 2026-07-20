import type { CanonicalAlertEnvelope } from "./types";

export type IncidentLifecycleState =
  | "opening"
  | "firing"
  | "resolving"
  | "suppressed"
  | "resolved";

export type QuotaAdmissionState =
  | "pending"
  | "confirmed"
  | "suppressed"
  | "released";

export type ReceiptAction =
  | "opened"
  | "repeated"
  | "resolving"
  | "orphan_resolution"
  | "stale"
  | "reopened"
  | "quota_suppressed"
  | "resolved_suppressed"
  | "queued_next_generation"
  | "cancelled_next_generation";

export interface LifecycleOrder {
  occurredAt: string;
  occurredAtMs: number;
  statusPrecedence: 0 | 1;
  eventId: string;
}

export interface ApplyEventAcknowledgement {
  accepted: true;
  incident_id: string | null;
  generation: number | null;
  lifecycle_state: IncidentLifecycleState | null;
  action: ReceiptAction;
  occurrence_count: number;
  state_version: number | null;
}

export interface NextGenerationCandidate {
  incidentId: string;
  generation: number;
  envelope: CanonicalAlertEnvelope;
  firstSeen: string;
  lastSeen: string;
  occurrenceCount: number;
  highWatermark: LifecycleOrder;
}

export interface IncidentGeneration {
  incidentId: string;
  generation: number;
  state: IncidentLifecycleState;
  stateVersion: number;
  resolutionEpoch: number;
  quotaState: QuotaAdmissionState;
  quotaLeaseEpoch: number | null;
  occurrenceCount: number;
  firstSeen: string;
  lastSeen: string;
  highWatermark: LifecycleOrder;
  latestEnvelope: CanonicalAlertEnvelope;
  resolutionEnvelope: CanonicalAlertEnvelope | null;
  slackThreadTs: string | null;
  nextGenerationCandidate: NextGenerationCandidate | null;
  createdAtMs: number;
  updatedAtMs: number;
}

export interface EventReceipt {
  eventId: string;
  bodyDigest: string;
  generation: number | null;
  action: ReceiptAction;
  receivedAtMs: number;
  acknowledgement: ApplyEventAcknowledgement;
}

export type PendingActionType =
  | "reserve_quota"
  | "release_quota"
  | "post_parent"
  | "update_parent"
  | "post_recovery"
  | "dispatch_analysis"
  | "post_analysis";

export type PendingActionStatus =
  | "blocked"
  | "pending"
  | "claimed"
  | "uncertain"
  | "completed"
  | "cancelled"
  | "failed"
  | "manual_reconciliation_required";

export interface PendingAction {
  actionId: string;
  incidentId: string;
  generation: number;
  type: PendingActionType;
  payload: Record<string, unknown>;
  status: PendingActionStatus;
  version: number;
  stateVersion: number;
  resolutionEpoch: number;
  attempt: number;
  claimEpoch: number;
  claimMode: "execute" | "reconcile" | null;
  startedAtMs: number | null;
  leaseExpiresAtMs: number | null;
  nextRunAtMs: number | null;
  reconcileAtMs: number | null;
  finalDeadlineAtMs: number;
  dependsOnActionId: string | null;
  lastError: string | null;
  result: Record<string, unknown> | null;
  createdAtMs: number;
  updatedAtMs: number;
}

export type AnalysisJobStatus =
  | "not_requested"
  | "queued"
  | "dispatched"
  | "succeeded"
  | "failed";

export interface AnalysisJob {
  jobId: string;
  incidentId: string;
  generation: number;
  version: number;
  status: AnalysisJobStatus;
  checkoutSha: string;
  executionLeaseNonce: string | null;
  githubRunId: string | null;
  githubRunAttempt: number | null;
  leaseExpiresAtMs: number | null;
  deadlineAtMs: number | null;
  completion: Record<string, unknown> | null;
  lastError: string | null;
  createdAtMs: number;
  updatedAtMs: number;
}

export interface SchedulerState {
  desiredAlarmAtMs: number | null;
  schedulerEpoch: number;
  lastRunAtMs: number | null;
  lastError: string | null;
  lifecycleHighWatermark: LifecycleOrder | null;
}

export interface IncidentStoreSnapshot {
  generations: IncidentGeneration[];
  receipts: EventReceipt[];
  actions: PendingAction[];
  analysisJobs: AnalysisJob[];
  scheduler: SchedulerState;
}

export interface IncidentStore {
  initializeSchema(): void;
  transaction<T>(callback: () => T): T;
  getReceipt(eventId: string): EventReceipt | null;
  putReceipt(receipt: EventReceipt): void;
  getGeneration(generation: number): IncidentGeneration | null;
  getLatestGeneration(): IncidentGeneration | null;
  putGeneration(generation: IncidentGeneration): void;
  getAction(actionId: string): PendingAction | null;
  listActions(): PendingAction[];
  putAction(action: PendingAction): void;
  getAnalysisJob(jobId: string): AnalysisJob | null;
  listAnalysisJobs(): AnalysisJob[];
  putAnalysisJob(job: AnalysisJob): void;
  getSchedulerState(): SchedulerState;
  putSchedulerState(state: SchedulerState): void;
  snapshot(): IncidentStoreSnapshot;
}

export const SQL_SCHEMA = [
  `CREATE TABLE IF NOT EXISTS incident_generations (
    generation INTEGER PRIMARY KEY,
    incident_id TEXT NOT NULL UNIQUE,
    lifecycle_state TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    resolution_epoch INTEGER NOT NULL,
    quota_state TEXT NOT NULL,
    quota_lease_epoch INTEGER,
    occurrence_count INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    high_watermark_at TEXT NOT NULL,
    high_watermark_at_ms INTEGER NOT NULL,
    high_watermark_status INTEGER NOT NULL,
    high_watermark_event_id TEXT NOT NULL,
    latest_envelope_json TEXT NOT NULL,
    resolution_envelope_json TEXT,
    slack_thread_ts TEXT,
    next_generation_candidate_json TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS event_receipts (
    event_id TEXT PRIMARY KEY,
    body_digest TEXT NOT NULL,
    generation INTEGER,
    action TEXT NOT NULL,
    received_at_ms INTEGER NOT NULL,
    acknowledgement_json TEXT NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS pending_actions (
    action_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    action_status TEXT NOT NULL,
    version INTEGER NOT NULL,
    state_version INTEGER NOT NULL,
    resolution_epoch INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    claim_epoch INTEGER NOT NULL,
    claim_mode TEXT,
    started_at_ms INTEGER,
    lease_expires_at_ms INTEGER,
    next_run_at_ms INTEGER,
    reconcile_at_ms INTEGER,
    final_deadline_at_ms INTEGER NOT NULL,
    depends_on_action_id TEXT,
    last_error TEXT,
    result_json TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS pending_actions_schedule
     ON pending_actions(action_status, next_run_at_ms, reconcile_at_ms, lease_expires_at_ms)`,
  `CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    generation INTEGER NOT NULL UNIQUE,
    version INTEGER NOT NULL,
    job_status TEXT NOT NULL,
    checkout_sha TEXT NOT NULL,
    execution_lease_nonce TEXT,
    github_run_id TEXT,
    github_run_attempt INTEGER,
    lease_expires_at_ms INTEGER,
    deadline_at_ms INTEGER,
    completion_json TEXT,
    last_error TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS scheduler_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    desired_alarm_at_ms INTEGER,
    scheduler_epoch INTEGER NOT NULL,
    last_run_at_ms INTEGER,
    last_error TEXT,
    lifecycle_high_watermark_at TEXT,
    lifecycle_high_watermark_at_ms INTEGER,
    lifecycle_high_watermark_status INTEGER,
    lifecycle_high_watermark_event_id TEXT
  )`,
  `INSERT OR IGNORE INTO scheduler_state (
    singleton, desired_alarm_at_ms, scheduler_epoch, last_run_at_ms, last_error,
    lifecycle_high_watermark_at, lifecycle_high_watermark_at_ms,
    lifecycle_high_watermark_status, lifecycle_high_watermark_event_id
  ) VALUES (1, NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL)`,
] as const;

type Row = Record<string, unknown>;

function clone<T>(value: T): T {
  return structuredClone(value);
}

function nullableString(value: unknown): string | null {
  return value === null || value === undefined ? null : String(value);
}

function nullableNumber(value: unknown): number | null {
  return value === null || value === undefined ? null : Number(value);
}

function parseJson<T>(value: unknown): T {
  return JSON.parse(String(value)) as T;
}

function parseLifecycleOrder(
  at: unknown,
  atMs: unknown,
  status: unknown,
  eventId: unknown,
): LifecycleOrder {
  return {
    occurredAt: String(at),
    occurredAtMs: Number(atMs),
    statusPrecedence: Number(status) as 0 | 1,
    eventId: String(eventId),
  };
}

function parseGeneration(row: Row): IncidentGeneration {
  return {
    incidentId: String(row.incident_id),
    generation: Number(row.generation),
    state: String(row.lifecycle_state) as IncidentLifecycleState,
    stateVersion: Number(row.state_version),
    resolutionEpoch: Number(row.resolution_epoch),
    quotaState: String(row.quota_state) as QuotaAdmissionState,
    quotaLeaseEpoch: nullableNumber(row.quota_lease_epoch),
    occurrenceCount: Number(row.occurrence_count),
    firstSeen: String(row.first_seen),
    lastSeen: String(row.last_seen),
    highWatermark: parseLifecycleOrder(
      row.high_watermark_at,
      row.high_watermark_at_ms,
      row.high_watermark_status,
      row.high_watermark_event_id,
    ),
    latestEnvelope: parseJson<CanonicalAlertEnvelope>(row.latest_envelope_json),
    resolutionEnvelope: row.resolution_envelope_json
      ? parseJson<CanonicalAlertEnvelope>(row.resolution_envelope_json)
      : null,
    slackThreadTs: nullableString(row.slack_thread_ts),
    nextGenerationCandidate: row.next_generation_candidate_json
      ? parseJson<NextGenerationCandidate>(row.next_generation_candidate_json)
      : null,
    createdAtMs: Number(row.created_at_ms),
    updatedAtMs: Number(row.updated_at_ms),
  };
}

function parseReceipt(row: Row): EventReceipt {
  return {
    eventId: String(row.event_id),
    bodyDigest: String(row.body_digest),
    generation: nullableNumber(row.generation),
    action: String(row.action) as ReceiptAction,
    receivedAtMs: Number(row.received_at_ms),
    acknowledgement: parseJson<ApplyEventAcknowledgement>(row.acknowledgement_json),
  };
}

function parseAction(row: Row): PendingAction {
  return {
    actionId: String(row.action_id),
    incidentId: String(row.incident_id),
    generation: Number(row.generation),
    type: String(row.action_type) as PendingActionType,
    payload: parseJson<Record<string, unknown>>(row.payload_json),
    status: String(row.action_status) as PendingActionStatus,
    version: Number(row.version),
    stateVersion: Number(row.state_version),
    resolutionEpoch: Number(row.resolution_epoch),
    attempt: Number(row.attempt),
    claimEpoch: Number(row.claim_epoch),
    claimMode: nullableString(row.claim_mode) as PendingAction["claimMode"],
    startedAtMs: nullableNumber(row.started_at_ms),
    leaseExpiresAtMs: nullableNumber(row.lease_expires_at_ms),
    nextRunAtMs: nullableNumber(row.next_run_at_ms),
    reconcileAtMs: nullableNumber(row.reconcile_at_ms),
    finalDeadlineAtMs: Number(row.final_deadline_at_ms),
    dependsOnActionId: nullableString(row.depends_on_action_id),
    lastError: nullableString(row.last_error),
    result: row.result_json ? parseJson<Record<string, unknown>>(row.result_json) : null,
    createdAtMs: Number(row.created_at_ms),
    updatedAtMs: Number(row.updated_at_ms),
  };
}

function parseAnalysisJob(row: Row): AnalysisJob {
  return {
    jobId: String(row.job_id),
    incidentId: String(row.incident_id),
    generation: Number(row.generation),
    version: Number(row.version),
    status: String(row.job_status) as AnalysisJobStatus,
    checkoutSha: String(row.checkout_sha),
    executionLeaseNonce: nullableString(row.execution_lease_nonce),
    githubRunId: nullableString(row.github_run_id),
    githubRunAttempt: nullableNumber(row.github_run_attempt),
    leaseExpiresAtMs: nullableNumber(row.lease_expires_at_ms),
    deadlineAtMs: nullableNumber(row.deadline_at_ms),
    completion: row.completion_json
      ? parseJson<Record<string, unknown>>(row.completion_json)
      : null,
    lastError: nullableString(row.last_error),
    createdAtMs: Number(row.created_at_ms),
    updatedAtMs: Number(row.updated_at_ms),
  };
}

function parseScheduler(row: Row | undefined): SchedulerState {
  if (!row) {
    return {
      desiredAlarmAtMs: null,
      schedulerEpoch: 0,
      lastRunAtMs: null,
      lastError: null,
      lifecycleHighWatermark: null,
    };
  }
  const watermark = row.lifecycle_high_watermark_at
    ? parseLifecycleOrder(
        row.lifecycle_high_watermark_at,
        row.lifecycle_high_watermark_at_ms,
        row.lifecycle_high_watermark_status,
        row.lifecycle_high_watermark_event_id,
      )
    : null;
  return {
    desiredAlarmAtMs: nullableNumber(row.desired_alarm_at_ms),
    schedulerEpoch: Number(row.scheduler_epoch),
    lastRunAtMs: nullableNumber(row.last_run_at_ms),
    lastError: nullableString(row.last_error),
    lifecycleHighWatermark: watermark,
  };
}

export class DurableObjectSqlStore implements IncidentStore {
  constructor(private readonly storage: DurableObjectStorage) {}

  initializeSchema(): void {
    for (const statement of SQL_SCHEMA) this.storage.sql.exec(statement);
  }

  transaction<T>(callback: () => T): T {
    return this.storage.transactionSync(callback);
  }

  private rows(query: string, ...bindings: (string | number | null)[]): Row[] {
    return Array.from(this.storage.sql.exec(query, ...bindings)) as Row[];
  }

  getReceipt(eventId: string): EventReceipt | null {
    const row = this.rows("SELECT * FROM event_receipts WHERE event_id = ?", eventId)[0];
    return row ? parseReceipt(row) : null;
  }

  putReceipt(receipt: EventReceipt): void {
    this.storage.sql.exec(
      `INSERT INTO event_receipts (
        event_id, body_digest, generation, action, received_at_ms, acknowledgement_json
      ) VALUES (?, ?, ?, ?, ?, ?)`,
      receipt.eventId,
      receipt.bodyDigest,
      receipt.generation,
      receipt.action,
      receipt.receivedAtMs,
      JSON.stringify(receipt.acknowledgement),
    );
  }

  getGeneration(generation: number): IncidentGeneration | null {
    const row = this.rows(
      "SELECT * FROM incident_generations WHERE generation = ?",
      generation,
    )[0];
    return row ? parseGeneration(row) : null;
  }

  getLatestGeneration(): IncidentGeneration | null {
    const row = this.rows(
      "SELECT * FROM incident_generations ORDER BY generation DESC LIMIT 1",
    )[0];
    return row ? parseGeneration(row) : null;
  }

  putGeneration(generation: IncidentGeneration): void {
    this.storage.sql.exec(
      `INSERT INTO incident_generations (
        generation, incident_id, lifecycle_state, state_version, resolution_epoch,
        quota_state, quota_lease_epoch, occurrence_count, first_seen, last_seen, high_watermark_at,
        high_watermark_at_ms, high_watermark_status, high_watermark_event_id,
        latest_envelope_json, resolution_envelope_json, slack_thread_ts,
        next_generation_candidate_json, created_at_ms, updated_at_ms
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(generation) DO UPDATE SET
        incident_id = excluded.incident_id,
        lifecycle_state = excluded.lifecycle_state,
        state_version = excluded.state_version,
        resolution_epoch = excluded.resolution_epoch,
        quota_state = excluded.quota_state,
        quota_lease_epoch = excluded.quota_lease_epoch,
        occurrence_count = excluded.occurrence_count,
        first_seen = excluded.first_seen,
        last_seen = excluded.last_seen,
        high_watermark_at = excluded.high_watermark_at,
        high_watermark_at_ms = excluded.high_watermark_at_ms,
        high_watermark_status = excluded.high_watermark_status,
        high_watermark_event_id = excluded.high_watermark_event_id,
        latest_envelope_json = excluded.latest_envelope_json,
        resolution_envelope_json = excluded.resolution_envelope_json,
        slack_thread_ts = excluded.slack_thread_ts,
        next_generation_candidate_json = excluded.next_generation_candidate_json,
        created_at_ms = excluded.created_at_ms,
        updated_at_ms = excluded.updated_at_ms`,
      generation.generation,
      generation.incidentId,
      generation.state,
      generation.stateVersion,
      generation.resolutionEpoch,
      generation.quotaState,
      generation.quotaLeaseEpoch,
      generation.occurrenceCount,
      generation.firstSeen,
      generation.lastSeen,
      generation.highWatermark.occurredAt,
      generation.highWatermark.occurredAtMs,
      generation.highWatermark.statusPrecedence,
      generation.highWatermark.eventId,
      JSON.stringify(generation.latestEnvelope),
      generation.resolutionEnvelope ? JSON.stringify(generation.resolutionEnvelope) : null,
      generation.slackThreadTs,
      generation.nextGenerationCandidate
        ? JSON.stringify(generation.nextGenerationCandidate)
        : null,
      generation.createdAtMs,
      generation.updatedAtMs,
    );
  }

  getAction(actionId: string): PendingAction | null {
    const row = this.rows("SELECT * FROM pending_actions WHERE action_id = ?", actionId)[0];
    return row ? parseAction(row) : null;
  }

  listActions(): PendingAction[] {
    return this.rows("SELECT * FROM pending_actions ORDER BY created_at_ms, action_id").map(
      parseAction,
    );
  }

  putAction(action: PendingAction): void {
    this.storage.sql.exec(
      `INSERT INTO pending_actions (
        action_id, incident_id, generation, action_type, payload_json, action_status,
        version, state_version, resolution_epoch, attempt, claim_epoch, claim_mode,
        started_at_ms, lease_expires_at_ms, next_run_at_ms, reconcile_at_ms,
        final_deadline_at_ms, depends_on_action_id, last_error, result_json,
        created_at_ms, updated_at_ms
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(action_id) DO UPDATE SET
        incident_id = excluded.incident_id,
        generation = excluded.generation,
        action_type = excluded.action_type,
        payload_json = excluded.payload_json,
        action_status = excluded.action_status,
        version = excluded.version,
        state_version = excluded.state_version,
        resolution_epoch = excluded.resolution_epoch,
        attempt = excluded.attempt,
        claim_epoch = excluded.claim_epoch,
        claim_mode = excluded.claim_mode,
        started_at_ms = excluded.started_at_ms,
        lease_expires_at_ms = excluded.lease_expires_at_ms,
        next_run_at_ms = excluded.next_run_at_ms,
        reconcile_at_ms = excluded.reconcile_at_ms,
        final_deadline_at_ms = excluded.final_deadline_at_ms,
        depends_on_action_id = excluded.depends_on_action_id,
        last_error = excluded.last_error,
        result_json = excluded.result_json,
        created_at_ms = excluded.created_at_ms,
        updated_at_ms = excluded.updated_at_ms`,
      action.actionId,
      action.incidentId,
      action.generation,
      action.type,
      JSON.stringify(action.payload),
      action.status,
      action.version,
      action.stateVersion,
      action.resolutionEpoch,
      action.attempt,
      action.claimEpoch,
      action.claimMode,
      action.startedAtMs,
      action.leaseExpiresAtMs,
      action.nextRunAtMs,
      action.reconcileAtMs,
      action.finalDeadlineAtMs,
      action.dependsOnActionId,
      action.lastError,
      action.result ? JSON.stringify(action.result) : null,
      action.createdAtMs,
      action.updatedAtMs,
    );
  }

  getAnalysisJob(jobId: string): AnalysisJob | null {
    const row = this.rows("SELECT * FROM analysis_jobs WHERE job_id = ?", jobId)[0];
    return row ? parseAnalysisJob(row) : null;
  }

  listAnalysisJobs(): AnalysisJob[] {
    return this.rows("SELECT * FROM analysis_jobs ORDER BY created_at_ms, job_id").map(
      parseAnalysisJob,
    );
  }

  putAnalysisJob(job: AnalysisJob): void {
    this.storage.sql.exec(
      `INSERT INTO analysis_jobs (
        job_id, incident_id, generation, version, job_status, checkout_sha,
        execution_lease_nonce, github_run_id, github_run_attempt, lease_expires_at_ms,
        deadline_at_ms, completion_json, last_error, created_at_ms, updated_at_ms
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(job_id) DO UPDATE SET
        incident_id = excluded.incident_id,
        generation = excluded.generation,
        version = excluded.version,
        job_status = excluded.job_status,
        checkout_sha = excluded.checkout_sha,
        execution_lease_nonce = excluded.execution_lease_nonce,
        github_run_id = excluded.github_run_id,
        github_run_attempt = excluded.github_run_attempt,
        lease_expires_at_ms = excluded.lease_expires_at_ms,
        deadline_at_ms = excluded.deadline_at_ms,
        completion_json = excluded.completion_json,
        last_error = excluded.last_error,
        created_at_ms = excluded.created_at_ms,
        updated_at_ms = excluded.updated_at_ms`,
      job.jobId,
      job.incidentId,
      job.generation,
      job.version,
      job.status,
      job.checkoutSha,
      job.executionLeaseNonce,
      job.githubRunId,
      job.githubRunAttempt,
      job.leaseExpiresAtMs,
      job.deadlineAtMs,
      job.completion ? JSON.stringify(job.completion) : null,
      job.lastError,
      job.createdAtMs,
      job.updatedAtMs,
    );
  }

  getSchedulerState(): SchedulerState {
    return parseScheduler(this.rows("SELECT * FROM scheduler_state WHERE singleton = 1")[0]);
  }

  putSchedulerState(state: SchedulerState): void {
    const watermark = state.lifecycleHighWatermark;
    this.storage.sql.exec(
      `INSERT INTO scheduler_state (
        singleton, desired_alarm_at_ms, scheduler_epoch, last_run_at_ms, last_error,
        lifecycle_high_watermark_at, lifecycle_high_watermark_at_ms,
        lifecycle_high_watermark_status, lifecycle_high_watermark_event_id
      ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(singleton) DO UPDATE SET
        desired_alarm_at_ms = excluded.desired_alarm_at_ms,
        scheduler_epoch = excluded.scheduler_epoch,
        last_run_at_ms = excluded.last_run_at_ms,
        last_error = excluded.last_error,
        lifecycle_high_watermark_at = excluded.lifecycle_high_watermark_at,
        lifecycle_high_watermark_at_ms = excluded.lifecycle_high_watermark_at_ms,
        lifecycle_high_watermark_status = excluded.lifecycle_high_watermark_status,
        lifecycle_high_watermark_event_id = excluded.lifecycle_high_watermark_event_id`,
      state.desiredAlarmAtMs,
      state.schedulerEpoch,
      state.lastRunAtMs,
      state.lastError,
      watermark?.occurredAt ?? null,
      watermark?.occurredAtMs ?? null,
      watermark?.statusPrecedence ?? null,
      watermark?.eventId ?? null,
    );
  }

  snapshot(): IncidentStoreSnapshot {
    return {
      generations: this.rows(
        "SELECT * FROM incident_generations ORDER BY generation",
      ).map(parseGeneration),
      receipts: this.rows("SELECT * FROM event_receipts ORDER BY received_at_ms, event_id").map(
        parseReceipt,
      ),
      actions: this.listActions(),
      analysisJobs: this.listAnalysisJobs(),
      scheduler: this.getSchedulerState(),
    };
  }
}

export class InMemoryIncidentStore implements IncidentStore {
  private generations = new Map<number, IncidentGeneration>();
  private receipts = new Map<string, EventReceipt>();
  private actions = new Map<string, PendingAction>();
  private analysisJobs = new Map<string, AnalysisJob>();
  private scheduler: SchedulerState = {
    desiredAlarmAtMs: null,
    schedulerEpoch: 0,
    lastRunAtMs: null,
    lastError: null,
    lifecycleHighWatermark: null,
  };

  initializeSchema(): void {}

  transaction<T>(callback: () => T): T {
    const before = this.snapshot();
    try {
      return callback();
    } catch (error) {
      this.restore(before);
      throw error;
    }
  }

  private restore(snapshot: IncidentStoreSnapshot): void {
    this.generations = new Map(
      snapshot.generations.map((generation) => [generation.generation, clone(generation)]),
    );
    this.receipts = new Map(
      snapshot.receipts.map((receipt) => [receipt.eventId, clone(receipt)]),
    );
    this.actions = new Map(
      snapshot.actions.map((action) => [action.actionId, clone(action)]),
    );
    this.analysisJobs = new Map(
      snapshot.analysisJobs.map((job) => [job.jobId, clone(job)]),
    );
    this.scheduler = clone(snapshot.scheduler);
  }

  getReceipt(eventId: string): EventReceipt | null {
    const receipt = this.receipts.get(eventId);
    return receipt ? clone(receipt) : null;
  }

  putReceipt(receipt: EventReceipt): void {
    if (this.receipts.has(receipt.eventId)) {
      throw new Error(`duplicate receipt: ${receipt.eventId}`);
    }
    this.receipts.set(receipt.eventId, clone(receipt));
  }

  getGeneration(generation: number): IncidentGeneration | null {
    const value = this.generations.get(generation);
    return value ? clone(value) : null;
  }

  getLatestGeneration(): IncidentGeneration | null {
    const latest = [...this.generations.keys()].sort((left, right) => right - left)[0];
    return latest === undefined ? null : clone(this.generations.get(latest)!);
  }

  putGeneration(generation: IncidentGeneration): void {
    this.generations.set(generation.generation, clone(generation));
  }

  getAction(actionId: string): PendingAction | null {
    const value = this.actions.get(actionId);
    return value ? clone(value) : null;
  }

  listActions(): PendingAction[] {
    return [...this.actions.values()]
      .map(clone)
      .sort(
        (left, right) =>
          left.createdAtMs - right.createdAtMs || left.actionId.localeCompare(right.actionId),
      );
  }

  putAction(action: PendingAction): void {
    this.actions.set(action.actionId, clone(action));
  }

  getAnalysisJob(jobId: string): AnalysisJob | null {
    const value = this.analysisJobs.get(jobId);
    return value ? clone(value) : null;
  }

  listAnalysisJobs(): AnalysisJob[] {
    return [...this.analysisJobs.values()]
      .map(clone)
      .sort(
        (left, right) =>
          left.createdAtMs - right.createdAtMs || left.jobId.localeCompare(right.jobId),
      );
  }

  putAnalysisJob(job: AnalysisJob): void {
    this.analysisJobs.set(job.jobId, clone(job));
  }

  getSchedulerState(): SchedulerState {
    return clone(this.scheduler);
  }

  putSchedulerState(state: SchedulerState): void {
    this.scheduler = clone(state);
  }

  snapshot(): IncidentStoreSnapshot {
    return {
      generations: [...this.generations.values()]
        .map(clone)
        .sort((left, right) => left.generation - right.generation),
      receipts: [...this.receipts.values()]
        .map(clone)
        .sort(
          (left, right) =>
            left.receivedAtMs - right.receivedAtMs || left.eventId.localeCompare(right.eventId),
        ),
      actions: this.listActions(),
      analysisJobs: this.listAnalysisJobs(),
      scheduler: this.getSchedulerState(),
    };
  }
}

export function computeDesiredAlarmAt(store: IncidentStore): number | null {
  const deadlines: number[] = [];
  for (const action of store.listActions()) {
    if (
      action.status === "blocked" ||
      action.status === "pending" ||
      action.status === "claimed" ||
      action.status === "uncertain"
    ) {
      deadlines.push(action.finalDeadlineAtMs);
    }
    if (action.status === "pending" && action.nextRunAtMs !== null) {
      deadlines.push(action.nextRunAtMs);
    } else if (action.status === "claimed" && action.leaseExpiresAtMs !== null) {
      deadlines.push(action.leaseExpiresAtMs);
    } else if (action.status === "uncertain" && action.reconcileAtMs !== null) {
      deadlines.push(action.reconcileAtMs);
    }
  }
  for (const job of store.listAnalysisJobs()) {
    if (
      job.deadlineAtMs !== null &&
      job.status !== "succeeded" &&
      job.status !== "failed"
    ) {
      deadlines.push(job.deadlineAtMs);
    }
    if (job.leaseExpiresAtMs !== null && job.status === "dispatched") {
      deadlines.push(job.leaseExpiresAtMs);
    }
  }
  return deadlines.length === 0 ? null : Math.min(...deadlines);
}

export function refreshDesiredAlarmAt(store: IncidentStore): number | null {
  const desiredAlarmAtMs = computeDesiredAlarmAt(store);
  const current = store.getSchedulerState();
  if (current.desiredAlarmAtMs !== desiredAlarmAtMs) {
    store.putSchedulerState({
      ...current,
      desiredAlarmAtMs,
      schedulerEpoch: current.schedulerEpoch + 1,
    });
  }
  return desiredAlarmAtMs;
}
