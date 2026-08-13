export type AlertStatus = "firing" | "resolved";
export type AlertSeverity = "critical" | "error" | "warn" | "info";
export type SupportedAlertType =
  | "provider_circuit_open"
  | "model_unavailable"
  | "monitoring_cycle_failure";

export type ProviderFailureReason =
  | "authentication"
  | "availability_below_threshold"
  | "connection_refused"
  | "error"
  | "rate_limited"
  | "timeout"
  | "unknown"
  | "upstream_error";

export interface ProviderCircuitContext {
  readonly provider: string;
  readonly availability?: number;
  readonly error?: string;
  readonly affected_users?: number;
  readonly consecutive_failures?: number;
  readonly final_failure_count?: number;
  readonly outage_duration_ms?: number;
  readonly reason?: ProviderFailureReason;
}

export type ModelUnavailabilityReason =
  | "authentication"
  | "rate_limited"
  | "timeout"
  | "unknown"
  | "upstream_error";

/** Platform-neutral context emitted by the status monitor for one model. */
export interface ModelUnavailableContext {
  readonly model_id: string;
  readonly consecutive_failures?: number;
  readonly failure_threshold?: number;
  readonly latency_ms?: number;
  readonly reason?: ModelUnavailabilityReason;
}

export type MonitoringCycleReason =
  | "account_rejected"
  | "discovery_failed"
  | "not_configured"
  | "unknown";

/**
 * Context for a whole-cycle monitoring failure (gateway unreachable, prober key
 * rejected account-wide, or the monitor unable to run at all). Deliberately a
 * closed reason enum with no free-text error: cycle errors embed upstream
 * response fragments, so the raw message stays on the monitor's own
 * dashboard/health surfaces instead of crossing the trust boundary.
 */
export interface MonitoringCycleContext {
  readonly reason?: MonitoringCycleReason;
}

interface AlertEventBase {
  readonly schema_version: 1;
  readonly event_id: string;
  readonly fingerprint: string;
  readonly status: AlertStatus;
  readonly severity: AlertSeverity;
  readonly title: string;
  readonly occurred_at: string;
  readonly summary: string;
  readonly evidence_refs: readonly string[];
}

export interface ProviderCircuitAlertEvent extends AlertEventBase {
  readonly alert_type: "provider_circuit_open";
  readonly context: ProviderCircuitContext;
}

export interface ModelUnavailableAlertEvent extends AlertEventBase {
  readonly alert_type: "model_unavailable";
  readonly context: ModelUnavailableContext;
}

export interface MonitoringCycleAlertEvent extends AlertEventBase {
  readonly alert_type: "monitoring_cycle_failure";
  readonly context: MonitoringCycleContext;
}

export type AlertEvent =
  | ProviderCircuitAlertEvent
  | ModelUnavailableAlertEvent
  | MonitoringCycleAlertEvent;

export type TrustedEnvironment = "staging" | "production";
export type TrustedSource = "gateway" | "status-monitor";

/**
 * Metadata derived from authenticated deployment identity, never from the producer body.
 *
 * `environment` and `target_environment` answer different questions and are not
 * interchangeable. `environment` is the *trust* domain: which GitHub Environment
 * signed the deployment attestation, which registry shard holds the record, and
 * which principal quota the producer spends. `target_environment` is the alert's
 * *subject*: the deployment this alert is about. They coincide for a producer
 * that alerts about itself (the gateway), and diverge for a prober — the status
 * monitor ships from `dev` through the staging pipeline while probing whichever
 * gateway its deployed config names.
 *
 * Both are fixed from the attested deployment record. Neither is ever read from
 * the producer body.
 */
export interface TrustedAlertMetadata {
  readonly environment: TrustedEnvironment;
  readonly target_environment: TrustedEnvironment;
  readonly source: TrustedSource;
  readonly principal: string;
  readonly deployment_id: string;
  readonly deployment_sha: string;
  readonly artifact_digest: string;
  readonly registry_version: number;
}

/** Internal control-plane value. This is not a second producer wire contract. */
export interface CanonicalAlertEnvelope {
  readonly event: AlertEvent;
  readonly trusted: TrustedAlertMetadata;
}

export interface SlackTextObject {
  readonly type: "mrkdwn" | "plain_text";
  readonly text: string;
  readonly emoji?: boolean;
}

export type SlackBlock =
  | { readonly type: "header"; readonly text: SlackTextObject }
  | {
      readonly type: "section";
      readonly text?: SlackTextObject;
      readonly fields?: readonly SlackTextObject[];
    }
  | { readonly type: "context"; readonly elements: readonly SlackTextObject[] }
  | { readonly type: "divider" };

export interface SlackMessageMetadata {
  readonly event_type: "alert_control_plane_action";
  readonly event_payload: {
    readonly action_id: string;
    readonly incident_id: string;
    readonly generation: number;
    readonly payload_digest: string;
  };
}

export interface SlackMessage {
  readonly text: string;
  readonly blocks: readonly SlackBlock[];
  readonly metadata: SlackMessageMetadata;
}
