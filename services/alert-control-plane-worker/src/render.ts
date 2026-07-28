import type {
  CanonicalAlertEnvelope,
  DependencyUnavailableContext,
  MetricThresholdContext,
  ModelUnavailableContext,
  MonitoringCycleContext,
  ProviderCircuitContext,
  SlackBlock,
  SlackMessage,
  SlackTextObject,
} from "./types";

const CONTROL_RE = /[\u0000-\u001f\u007f-\u009f]/gu;

export interface IncidentRenderState {
  readonly action_id: string;
  readonly incident_id: string;
  readonly generation: number;
  readonly payload_digest: string;
  readonly occurrence_count: number;
  readonly first_seen: string;
  readonly last_seen: string;
}

function truncate(value: string, limit: number): string {
  const characters = [...value];
  if (characters.length <= limit) return value;
  return `${characters.slice(0, Math.max(0, limit - 1)).join("")}…`;
}

function plain(value: string, limit = 150): string {
  return truncate(value.replace(CONTROL_RE, " ").trim(), limit);
}

/** Escape all Slack mrkdwn entities, including encoded user/channel mentions. */
export function escapeSlackMrkdwn(value: string, limit = 3_000): string {
  const escaped = value
    .replace(CONTROL_RE, " ")
    .trim()
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
  return truncate(escaped, limit);
}

function mrkdwn(text: string): SlackTextObject {
  return { type: "mrkdwn", text: truncate(text, 3_000) };
}

function field(label: string, value: string): SlackTextObject {
  return mrkdwn(`*${label}*\n${truncate(value, 1_900)}`);
}

function environmentLabel(envelope: CanonicalAlertEnvelope): string {
  return envelope.trusted.environment.toUpperCase();
}

function severityIcon(severity: CanonicalAlertEnvelope["event"]["severity"]): string {
  if (severity === "critical") return "🚨";
  if (severity === "error") return "❌";
  if (severity === "warn") return "⚠️";
  return "ℹ️";
}

function deploymentLabel(envelope: CanonicalAlertEnvelope): string {
  return `${envelope.trusted.deployment_id}@${envelope.trusted.deployment_sha.slice(0, 12)}`;
}

function optionalField(
  fields: SlackTextObject[],
  label: string,
  value: string | number | undefined,
): void {
  if (value === undefined) return;
  fields.push(field(label, escapeSlackMrkdwn(String(value), 1_500)));
}

function providerContextFields(context: ProviderCircuitContext): readonly SlackTextObject[] {
  const fields: SlackTextObject[] = [
    field("Provider", escapeSlackMrkdwn(context.provider, 500)),
  ];
  if (context.availability !== undefined) {
    optionalField(fields, "Availability", `${(context.availability * 100).toFixed(1)}%`);
  }
  optionalField(fields, "Reason", context.reason);
  optionalField(fields, "Consecutive failures", context.consecutive_failures);
  optionalField(fields, "Final failure count", context.final_failure_count);
  optionalField(fields, "Affected users", context.affected_users);
  if (context.outage_duration_ms !== undefined) {
    optionalField(fields, "Outage duration", formatDuration(context.outage_duration_ms));
  }
  optionalField(fields, "Error", context.error);
  return fields.slice(0, 10);
}

function modelUnavailableContextFields(
  context: ModelUnavailableContext,
): readonly SlackTextObject[] {
  const fields: SlackTextObject[] = [
    field("Model", escapeSlackMrkdwn(context.model_id, 500)),
  ];
  optionalField(fields, "Reason", context.reason);
  optionalField(fields, "Consecutive failures", context.consecutive_failures);
  optionalField(fields, "Failure threshold", context.failure_threshold);
  if (context.latency_ms !== undefined) {
    optionalField(fields, "Last latency", `${Math.round(context.latency_ms)}ms`);
  }
  return fields;
}

function monitoringCycleContextFields(
  context: MonitoringCycleContext,
): readonly SlackTextObject[] {
  const fields: SlackTextObject[] = [];
  optionalField(fields, "Reason", context.reason);
  return fields;
}

/** Human labels for the metric enum; the wire keeps the stable identifier. */
const METRIC_LABELS: Readonly<Record<string, string>> = {
  auth_failure_count: "Auth failures",
  failed_request_rate: "Failed-request rate",
  http_5xx_rate: "5xx rate",
  latency_p95_ms: "p95 latency",
  prefix_cache_pending_evictions: "Prefix-cache evictions",
  provider_hourly_spend: "Hourly spend",
  tracked_task_failure_rate: "Tracked-task failure rate",
  user_daily_cost: "Daily cost",
};

function metricThresholdContextFields(
  context: MetricThresholdContext,
): readonly SlackTextObject[] {
  const fields: SlackTextObject[] = [
    field("Metric", METRIC_LABELS[context.metric] ?? context.metric),
  ];
  optionalField(fields, "Observed", context.observed);
  optionalField(fields, "Threshold", context.threshold);
  if (context.window_sec !== undefined) {
    optionalField(fields, "Window", formatDuration(context.window_sec * 1_000));
  }
  optionalField(fields, "Scope", context.scope);
  optionalField(fields, "Subject", context.subject);
  optionalField(fields, "Samples", context.sample_count);
  // Addresses are what on-call blocks, so they are rendered in full; the
  // validator has already proven every entry is an IP and nothing else.
  if (context.source_addresses !== undefined) {
    optionalField(fields, "Source addresses", context.source_addresses.join(", "));
  }
  optionalField(fields, "Distinct sources", context.distinct_sources);
  if (context.top_source_share !== undefined) {
    optionalField(
      fields,
      "Top source share",
      `${(context.top_source_share * 100).toFixed(1)}%`,
    );
  }
  return fields.slice(0, 10);
}

function dependencyUnavailableContextFields(
  context: DependencyUnavailableContext,
): readonly SlackTextObject[] {
  const fields: SlackTextObject[] = [field("Dependency", context.dependency)];
  optionalField(fields, "Backend", context.backend);
  return fields;
}

function contextFields(envelope: CanonicalAlertEnvelope): readonly SlackTextObject[] {
  switch (envelope.event.alert_type) {
    case "provider_circuit_open":
      return providerContextFields(envelope.event.context);
    case "model_unavailable":
      return modelUnavailableContextFields(envelope.event.context);
    case "monitoring_cycle_failure":
      return monitoringCycleContextFields(envelope.event.context);
    case "metric_threshold_breach":
      return metricThresholdContextFields(envelope.event.context);
    case "dependency_unavailable":
      return dependencyUnavailableContextFields(envelope.event.context);
  }
}

function recoveryContextFields(envelope: CanonicalAlertEnvelope): readonly SlackTextObject[] {
  if (envelope.event.alert_type !== "provider_circuit_open") return [];
  const fields: SlackTextObject[] = [];
  optionalField(fields, "Final failure count", envelope.event.context.final_failure_count);
  if (envelope.event.context.outage_duration_ms !== undefined) {
    optionalField(fields, "Outage duration", formatDuration(envelope.event.context.outage_duration_ms));
  }
  return fields;
}

function formatDuration(milliseconds: number): string {
  let seconds = Math.max(0, Math.floor(milliseconds / 1_000));
  const days = Math.floor(seconds / 86_400);
  seconds %= 86_400;
  const hours = Math.floor(seconds / 3_600);
  seconds %= 3_600;
  const minutes = Math.floor(seconds / 60);
  seconds %= 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function metadata(state: IncidentRenderState): SlackMessage["metadata"] {
  return {
    event_type: "alert_control_plane_action",
    event_payload: {
      action_id: state.action_id,
      incident_id: state.incident_id,
      generation: state.generation,
      payload_digest: state.payload_digest,
    },
  };
}

/** Render the single parent message from a validated internal envelope. */
export function renderParent(
  envelope: CanonicalAlertEnvelope,
  state: IncidentRenderState,
): SlackMessage {
  const resolved = envelope.event.status === "resolved";
  const lifecycle = resolved ? "Resolved" : "Firing";
  const environment = environmentLabel(envelope);
  const context = contextFields(envelope);
  const blocks: SlackBlock[] = [
    {
      type: "header",
      text: {
        type: "plain_text",
        text: plain(
          `${resolved ? "✅" : severityIcon(envelope.event.severity)} ${environment} · ${lifecycle}`,
        ),
        emoji: true,
      },
    },
    {
      type: "section",
      text: mrkdwn(
        `*${escapeSlackMrkdwn(envelope.event.title, 500)}*\n${escapeSlackMrkdwn(envelope.event.summary)}`,
      ),
    },
    {
      type: "section",
      fields: [
        field("Environment", environment),
        field("Severity", envelope.event.severity.toUpperCase()),
        field("Incident", escapeSlackMrkdwn(state.incident_id, 128)),
        field("Generation", String(state.generation)),
        field("Deployment", escapeSlackMrkdwn(deploymentLabel(envelope), 300)),
        field("First seen", escapeSlackMrkdwn(state.first_seen, 64)),
        field("Last seen", escapeSlackMrkdwn(state.last_seen, 64)),
        field("Occurrences", String(state.occurrence_count)),
      ],
    },
    ...(context.length > 0 ? [{ type: "section" as const, fields: context }] : []),
    {
      type: "context",
      elements: [
        mrkdwn(
          `Source: ${escapeSlackMrkdwn(envelope.trusted.source, 64)} · ` +
            `Fingerprint: ${escapeSlackMrkdwn(envelope.event.fingerprint, 512)}`,
        ),
      ],
    },
  ];
  return {
    text: truncate(
      `[${environment}] ${lifecycle.toUpperCase()} · ` +
        `${escapeSlackMrkdwn(envelope.event.title, 300)} · Incident ${escapeSlackMrkdwn(state.incident_id, 128)}`,
      4_000,
    ),
    blocks,
    metadata: metadata(state),
  };
}

/** Render the ordered recovery reply; callers must only use a resolved envelope. */
export function renderRecoveryReply(
  envelope: CanonicalAlertEnvelope,
  state: IncidentRenderState,
): SlackMessage {
  if (envelope.event.status !== "resolved") {
    throw new Error("recovery replies require a resolved event");
  }
  const environment = environmentLabel(envelope);
  const recoveryFields: SlackTextObject[] = [
    field("Incident", escapeSlackMrkdwn(state.incident_id, 128)),
    field("Generation", String(state.generation)),
    field("Resolved", escapeSlackMrkdwn(state.last_seen, 64)),
    ...recoveryContextFields(envelope),
  ];
  return {
    text: truncate(
      `[${environment}] RESOLVED · ${escapeSlackMrkdwn(envelope.event.title, 300)} · ` +
        `Incident ${escapeSlackMrkdwn(state.incident_id, 128)}`,
      4_000,
    ),
    blocks: [
      {
        type: "header",
        text: { type: "plain_text", text: "✅ Recovery confirmed", emoji: true },
      },
      {
        type: "section",
        text: mrkdwn(
          `*${escapeSlackMrkdwn(envelope.event.title, 500)}*\n${escapeSlackMrkdwn(envelope.event.summary)}`,
        ),
      },
      { type: "section", fields: recoveryFields },
    ],
    metadata: metadata(state),
  };
}

/** Render a fenced Codex analysis as a thread reply without trusting mrkdwn input. */
export function renderAnalysisReply(
  analysis: Readonly<Record<string, unknown>>,
  state: IncidentRenderState,
): SlackMessage {
  const serialized = JSON.stringify(analysis);
  if (serialized === undefined) throw new Error("analysis must be JSON serializable");
  const safeAnalysis = escapeSlackMrkdwn(serialized, 2_700);
  return {
    text: truncate(`Incident ${escapeSlackMrkdwn(state.incident_id, 128)} analysis available`, 4_000),
    blocks: [
      {
        type: "header",
        text: { type: "plain_text", text: "🧠 Incident analysis", emoji: true },
      },
      {
        type: "section",
        text: mrkdwn(`*Codex analysis*\n\`${safeAnalysis}\``),
      },
      {
        type: "context",
        elements: [
          mrkdwn(
            `Incident: ${escapeSlackMrkdwn(state.incident_id, 128)} · ` +
              `Generation: ${state.generation}`,
          ),
        ],
      },
    ],
    metadata: metadata(state),
  };
}
