export type AlertStatus = "firing" | "resolved";
export type AlertSeverity = "critical" | "error" | "warn" | "info";
export type SupportedAlertType = "provider_circuit_open";

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

export type AlertEvent = ProviderCircuitAlertEvent;

export type TrustedEnvironment = "staging" | "production";
export type TrustedSource = "gateway" | "status-monitor";

/** Metadata derived from authenticated deployment identity, never from the producer body. */
export interface TrustedAlertMetadata {
  readonly environment: TrustedEnvironment;
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
  };
}

export interface SlackMessage {
  readonly text: string;
  readonly blocks: readonly SlackBlock[];
  readonly metadata: SlackMessageMetadata;
}
