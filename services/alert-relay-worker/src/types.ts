export type TrustedEnvironment = "staging" | "production" | "local";
export type AlertStatus = "firing" | "resolved";
export type AlertSeverity = "critical" | "error" | "warn" | "info";
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export interface AlertEventV2 {
  version: "2";
  alert_id: string;
  fingerprint: string;
  source: string;
  status: AlertStatus;
  severity: AlertSeverity;
  title: string;
  occurred_at: string;
  summary: string;
  context: Record<string, JsonValue>;
  deployment_sha?: string;
  evidence_refs?: string[];
}

export interface OnCallAnalysis {
  summary: string;
  classification:
    | "code_bug"
    | "upstream_provider"
    | "configuration"
    | "capacity"
    | "authentication"
    | "unknown";
  confidence: number;
  impact: string;
  evidence: string[];
  likely_cause: string;
  recommended_actions: string[];
  issue_recommendation: "none" | "create";
  draft_pr_recommendation: "none" | "create";
}

export interface SuccessfulCompletion {
  status: "success";
  analysis: OnCallAnalysis;
  codex_thread_id?: string;
}

export interface FailedCompletion {
  status: "failure";
  error: string;
  run_url: string;
}

export type JobCompletion = SuccessfulCompletion | FailedCompletion;

export type IncidentStatus = "opening" | "firing" | "resolved";
export type CodexStatus = "investigating" | "analysis_ready" | "unavailable" | "resolved";
export type JobStatus =
  | "waiting"
  | "queued"
  | "dispatching"
  | "dispatched"
  | "completing"
  | "completed"
  | "failed";

export interface Incident {
  id: string;
  environment: TrustedEnvironment;
  fingerprint: string;
  status: IncidentStatus;
  alert: AlertEventV2;
  resolutionAlert: AlertEventV2 | null;
  occurrenceCount: number;
  firstSeen: string;
  lastSeen: string;
  slackChannelId: string;
  slackThreadTs: string | null;
  codexStatus: CodexStatus;
  analysisRef: string | null;
  parentDirty: boolean;
  parentVersion: number;
  recoveryPending: boolean;
  recoveryMessageId: string | null;
}

export interface AlertReceipt {
  alertId: string;
  incidentId: string | null;
  action: "opened" | "repeated" | "resolved" | "orphan_resolution";
  receivedAt: string;
}

export interface AnalysisJob {
  id: string;
  incidentId: string;
  status: JobStatus;
  analysisRef: string | null;
  attempts: number;
  lastError: string | null;
  completion: JobCompletion | null;
}

export interface JobQueueMessage {
  job_id: string;
}

export interface SubmitAlertResult {
  accepted: true;
  duplicate: boolean;
  incident_id: string | null;
  status: AlertStatus;
  occurrence_count: number;
}

export interface CompleteJobResult {
  accepted: true;
  duplicate: boolean;
  status: "completed" | "failed";
}

export interface SlackTextObject {
  type: "mrkdwn" | "plain_text";
  text: string;
  emoji?: boolean;
}

export type SlackBlock =
  | { type: "header"; text: SlackTextObject }
  | { type: "section"; text?: SlackTextObject; fields?: SlackTextObject[] }
  | { type: "context"; elements: SlackTextObject[] }
  | { type: "divider" };

export interface SlackMessage {
  text: string;
  blocks: SlackBlock[];
}

/** Bindings and settings for the relay Worker. All secret values are optional at type level. */
export interface Env {
  DB: D1Database;
  ALERT_JOBS: Queue<JobQueueMessage>;
  SLACK_BOT_TOKEN?: string;
  SLACK_CHANNEL_ID?: string;
  ALERT_RELAY_V2_STAGING_TOKEN?: string;
  ALERT_RELAY_V2_PRODUCTION_TOKEN?: string;
  ALERT_RELAY_V2_LOCAL_TOKEN?: string;
  ALERT_RELAY_V2_WORKFLOW_TOKEN?: string;
  GITHUB_TOKEN?: string;
  GITHUB_REPOSITORY?: string;
  GITHUB_API_BASE_URL?: string;
  GITHUB_WORKFLOW_FILE?: string;
  CODEX_MODEL?: string;
  MODEL_BASE_URL?: string;
}
