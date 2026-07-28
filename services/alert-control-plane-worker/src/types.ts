export type AlertStatus = "firing" | "resolved";
export type AlertSeverity = "critical" | "error" | "warn" | "info";
export type SupportedAlertType =
  | "provider_circuit_open"
  | "model_unavailable"
  | "monitoring_cycle_failure"
  | "metric_threshold_breach"
  | "dependency_unavailable";

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

/**
 * Metrics the gateway can breach a threshold on. Grouped by *shape* rather
 * than by alert name: eight distinct backend alerts share one "an observed
 * value crossed its threshold over a window" structure, so they share one
 * type and one renderer. A new gateway alert of this shape adds an enum
 * member, not a new alert type.
 */
export type BreachedMetric =
  | "auth_failure_count"
  | "failed_request_rate"
  | "http_5xx_rate"
  | "latency_p95_ms"
  | "prefix_cache_pending_evictions"
  | "provider_hourly_spend"
  | "tracked_task_failure_rate"
  | "user_daily_cost";

/** What the breach is scoped to. Never carries the identifier itself. */
export type BreachScope = "gateway" | "provider" | "task" | "user";

/**
 * Structured replacement for the backend's free-text alert context.
 *
 * Today every context value is interpolated verbatim into the Slack message,
 * so on-call reads attacker IPs and the offending user id straight out of the
 * alert and acts on them. The migration must not take that away — but it also
 * must not reintroduce free text, which is how secrets and injection payloads
 * leak. So the operationally load-bearing values get *typed, bounded* fields
 * ({@link subject}, {@link source_addresses}) that reject anything not of
 * their exact shape, and only the incidental values become counts.
 *
 * Deliberately absent: API key prefixes (credential material, and blocking is
 * done by address anyway) and the pre-formatted "top paths / top status codes"
 * strings (triage colour available on the dashboard, and unbounded free text).
 */
export interface MetricThresholdContext {
  readonly metric: BreachedMetric;
  readonly observed: number;
  readonly threshold: number;
  readonly window_sec?: number;
  readonly scope?: BreachScope;
  /**
   * The provider, task, or user the breach is scoped to. A bounded identifier,
   * never free text — this is what on-call needs to act on a scoped breach.
   */
  readonly subject?: string;
  /**
   * Addresses driving the breach, for alerts where blocking them is the
   * response (auth failure spikes). Each entry must parse as an IP address, so
   * this field cannot smuggle arbitrary text the way the old context could.
   */
  readonly source_addresses?: readonly string[];
  /** Distinct sources seen, when listing them all would be unbounded. */
  readonly distinct_sources?: number;
  /** Share of the observation attributable to the largest single source, 0..1. */
  readonly top_source_share?: number;
  readonly sample_count?: number;
}

/** Gateway dependencies whose loss is alertable. */
export type UnavailableDependency = "log_store" | "operational_store";

/**
 * Why the dependency is unreachable. The backend currently sends this as a
 * free-text `error` string, which is where a DSN or host would leak; a closed
 * enum keeps the triage signal on-call actually uses without the text.
 */
export type DependencyFailureReason =
  | "authentication"
  | "connection_refused"
  | "health_check_failed"
  | "timeout"
  | "unknown";

export interface DependencyUnavailableContext {
  readonly dependency: UnavailableDependency;
  /** Backend implementation label (e.g. "postgres"); never a connection string. */
  readonly backend?: string;
  readonly reason?: DependencyFailureReason;
}

export interface MetricThresholdAlertEvent extends AlertEventBase {
  readonly alert_type: "metric_threshold_breach";
  readonly context: MetricThresholdContext;
}

export interface DependencyUnavailableAlertEvent extends AlertEventBase {
  readonly alert_type: "dependency_unavailable";
  readonly context: DependencyUnavailableContext;
}

export type AlertEvent =
  | ProviderCircuitAlertEvent
  | ModelUnavailableAlertEvent
  | MonitoringCycleAlertEvent
  | MetricThresholdAlertEvent
  | DependencyUnavailableAlertEvent;

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
    readonly payload_digest: string;
  };
}

export interface SlackMessage {
  readonly text: string;
  readonly blocks: readonly SlackBlock[];
  readonly metadata: SlackMessageMetadata;
}
