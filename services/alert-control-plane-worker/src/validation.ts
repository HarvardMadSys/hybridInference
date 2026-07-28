import type {
  AlertEvent,
  AlertSeverity,
  AlertStatus,
  BreachedMetric,
  BreachScope,
  CanonicalAlertEnvelope,
  DependencyUnavailableContext,
  MetricThresholdContext,
  ModelUnavailableContext,
  ModelUnavailabilityReason,
  UnavailableDependency,
  MonitoringCycleContext,
  MonitoringCycleReason,
  ProviderCircuitContext,
  ProviderFailureReason,
  TrustedAlertMetadata,
  TrustedEnvironment,
  TrustedSource,
} from "./types";

const EVENT_KEYS = new Set([
  "schema_version",
  "event_id",
  "alert_type",
  "fingerprint",
  "status",
  "severity",
  "title",
  "occurred_at",
  "summary",
  "context",
  "evidence_refs",
]);

const TRUSTED_METADATA_KEYS = new Set([
  "environment",
  "source",
  "principal",
  "deployment_id",
  "deployment_sha",
  "artifact_digest",
  "registry_version",
]);

const PRODUCER_FORBIDDEN_KEYS = new Set([
  "artifact_digest",
  "channel",
  "deployment",
  "deployment_id",
  "deployment_sha",
  "environment",
  "principal",
  "registry_version",
  "slack_blocks",
  "slack_channel",
  "slack_channel_id",
  "slack_text",
  "source",
  "thread_ts",
  "trusted",
]);

const PROVIDER_CONTEXT_KEYS = new Set([
  "provider",
  "availability",
  "error",
  "affected_users",
  "consecutive_failures",
  "final_failure_count",
  "outage_duration_ms",
  "reason",
]);

const MODEL_UNAVAILABLE_CONTEXT_KEYS = new Set([
  "model_id",
  "consecutive_failures",
  "failure_threshold",
  "latency_ms",
  "reason",
]);

const MONITORING_CYCLE_CONTEXT_KEYS = new Set(["reason"]);

const METRIC_THRESHOLD_CONTEXT_KEYS = new Set([
  "metric",
  "observed",
  "threshold",
  "window_sec",
  "scope",
  "subject",
  "source_addresses",
  "distinct_sources",
  "top_source_share",
  "sample_count",
]);

const MAX_SOURCE_ADDRESSES = 5;
const IPV4_STRICT_RE = /^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/;

const DEPENDENCY_CONTEXT_KEYS = new Set(["dependency", "backend"]);

const UNSAFE_CONTROL_RE =
  /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/u;
const RFC3339_RE =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/;
const SECRET_PATTERNS: readonly RegExp[] = [
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/i,
  /\b(?:api[_-]?key|authorization|cookie|password|secret|token)\s*[:=]\s*[^\s,;]+/i,
  /\bhyi-[A-Za-z0-9_-]{20,}\b/,
  /\b(?:gh[oprsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/i,
  /\bAKIA[0-9A-Z]{16}\b/,
  /\b(?:sk|gsk|xai|rk)[-_][A-Za-z0-9._-]{6,}/i,
  /\bAIza[0-9A-Za-z_-]{10,}/i,
  /-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----/i,
  /https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9/_-]+/i,
];
const PROMPT_INJECTION_PATTERNS: readonly RegExp[] = [
  /\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+instructions?\b/i,
  /\b(?:reveal|print|repeat|expose)\s+(?:the\s+)?(?:system|developer)\s+prompt\b/i,
  /\b(?:system|developer|assistant)\s+(?:prompt|message)\s*:/i,
  /\byou\s+are\s+(?:chatgpt|codex|an?\s+ai)\b/i,
  /<\|(?:system|assistant|developer|tool)\|>/i,
  /\b(?:begin|end)\s+(?:system|developer|instructions?)\b/i,
];
const EMAIL_RE = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i;
const IPV4_RE =
  /(?:^|[^\d])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:$|[^\d])/;
const IDENTIFIER_RE = /^[A-Za-z0-9._:-]+$/;
const FULL_SHA_RE = /^[A-Fa-f0-9]{40}$/;
const ARTIFACT_DIGEST_RE = /^sha256:[A-Fa-f0-9]{64}$/;
const DEFAULT_MAX_FUTURE_SKEW_MS = 5 * 60 * 1_000;

export interface ParseAlertOptions {
  readonly now?: Date | number;
  readonly maxFutureSkewMs?: number;
}

export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ValidationError(`${field} must be an object`);
  }
  return value as Record<string, unknown>;
}

function strictKeys(value: Record<string, unknown>, allowed: Set<string>, field: string): void {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new ValidationError(`${field} contains unsupported field: ${key}`);
    }
  }
}

function stringValue(
  value: unknown,
  field: string,
  maxLength: number,
  pattern?: RegExp,
): string {
  if (typeof value !== "string") {
    throw new ValidationError(`${field} must be a string`);
  }
  if (UNSAFE_CONTROL_RE.test(value)) {
    throw new ValidationError(`${field} contains a control character`);
  }
  const normalized = value.normalize("NFC").trim();
  if (!normalized) throw new ValidationError(`${field} must not be blank`);
  if ([...normalized].length > maxLength) {
    throw new ValidationError(`${field} must be at most ${maxLength} characters`);
  }
  if (pattern && !pattern.test(normalized)) {
    throw new ValidationError(`${field} has an invalid format`);
  }
  return normalized;
}

function untrustedString(value: unknown, field: string, maxLength: number): string {
  const parsed = stringValue(value, field, maxLength);
  if (SECRET_PATTERNS.some((pattern) => pattern.test(parsed))) {
    throw new ValidationError(`${field} contains secret material`);
  }
  if (EMAIL_RE.test(parsed) || containsIpAddress(parsed)) {
    throw new ValidationError(`${field} contains a user or network identifier`);
  }
  if (PROMPT_INJECTION_PATTERNS.some((pattern) => pattern.test(parsed))) {
    throw new ValidationError(`${field} contains control instructions`);
  }
  return parsed;
}

function containsIpAddress(value: string): boolean {
  if (IPV4_RE.test(value)) return true;
  for (const token of value.split(/[^0-9A-Fa-f:.]+/u)) {
    if (!token.includes(":")) continue;
    try {
      const parsed = new URL(`http://[${token}]/`);
      if (parsed.hostname.length > 0) return true;
    } catch {
      // Not a syntactically valid IPv6 address.
    }
  }
  return false;
}

function enumValue<T extends string>(
  value: unknown,
  field: string,
  allowed: readonly T[],
): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    throw new ValidationError(`${field} is invalid`);
  }
  return value as T;
}

function boundedNumber(
  value: unknown,
  field: string,
  minimum: number,
  maximum: number,
): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ValidationError(`${field} must be a finite number`);
  }
  if (value < minimum || value > maximum) {
    throw new ValidationError(`${field} must be between ${minimum} and ${maximum}`);
  }
  return value;
}

function boundedInteger(
  value: unknown,
  field: string,
  minimum: number,
  maximum: number,
): number {
  const parsed = boundedNumber(value, field, minimum, maximum);
  if (!Number.isSafeInteger(parsed)) {
    throw new ValidationError(`${field} must be an integer`);
  }
  return parsed;
}

function optional<T>(
  input: Record<string, unknown>,
  key: string,
  parser: (value: unknown) => T,
): T | undefined {
  if (!Object.hasOwn(input, key)) return undefined;
  return parser(input[key]);
}

function parseProviderCircuitContext(value: unknown): ProviderCircuitContext {
  const input = record(value, "context");
  strictKeys(input, PROVIDER_CONTEXT_KEYS, "context");

  const context: {
    provider: string;
    availability?: number;
    error?: string;
    affected_users?: number;
    consecutive_failures?: number;
    final_failure_count?: number;
    outage_duration_ms?: number;
    reason?: ProviderFailureReason;
  } = {
    provider: untrustedString(input.provider, "context.provider", 256),
  };

  context.availability = optional(input, "availability", (item) =>
    boundedNumber(item, "context.availability", 0, 1),
  );
  context.error = optional(input, "error", (item) =>
    untrustedString(item, "context.error", 2_000),
  );
  context.affected_users = optional(input, "affected_users", (item) =>
    boundedInteger(item, "context.affected_users", 0, 1_000_000_000),
  );
  context.consecutive_failures = optional(input, "consecutive_failures", (item) =>
    boundedInteger(item, "context.consecutive_failures", 0, 1_000_000_000),
  );
  context.final_failure_count = optional(input, "final_failure_count", (item) =>
    boundedInteger(item, "context.final_failure_count", 0, 1_000_000_000),
  );
  context.outage_duration_ms = optional(input, "outage_duration_ms", (item) =>
    boundedInteger(item, "context.outage_duration_ms", 0, 365 * 24 * 60 * 60 * 1_000),
  );
  context.reason = optional(input, "reason", (item) =>
    enumValue<ProviderFailureReason>(item, "context.reason", [
      "authentication",
      "availability_below_threshold",
      "connection_refused",
      "error",
      "rate_limited",
      "timeout",
      "unknown",
      "upstream_error",
    ]),
  );

  for (const key of Object.keys(context) as Array<keyof typeof context>) {
    if (context[key] === undefined) delete context[key];
  }
  return context;
}

function parseModelUnavailableContext(value: unknown): ModelUnavailableContext {
  const input = record(value, "context");
  strictKeys(input, MODEL_UNAVAILABLE_CONTEXT_KEYS, "context");

  const context: {
    model_id: string;
    consecutive_failures?: number;
    failure_threshold?: number;
    latency_ms?: number;
    reason?: ModelUnavailabilityReason;
  } = {
    model_id: untrustedString(input.model_id, "context.model_id", 256),
  };
  context.consecutive_failures = optional(input, "consecutive_failures", (item) =>
    boundedInteger(item, "context.consecutive_failures", 0, 1_000_000_000),
  );
  context.failure_threshold = optional(input, "failure_threshold", (item) =>
    boundedInteger(item, "context.failure_threshold", 1, 1_000_000_000),
  );
  context.latency_ms = optional(input, "latency_ms", (item) =>
    boundedNumber(item, "context.latency_ms", 0, 24 * 60 * 60 * 1_000),
  );
  context.reason = optional(input, "reason", (item) =>
    enumValue<ModelUnavailabilityReason>(item, "context.reason", [
      "authentication",
      "rate_limited",
      "timeout",
      "unknown",
      "upstream_error",
    ]),
  );

  for (const key of Object.keys(context) as Array<keyof typeof context>) {
    if (context[key] === undefined) delete context[key];
  }
  return context;
}

function parseMonitoringCycleContext(value: unknown): MonitoringCycleContext {
  const input = record(value, "context");
  strictKeys(input, MONITORING_CYCLE_CONTEXT_KEYS, "context");

  const reason = optional(input, "reason", (item) =>
    enumValue<MonitoringCycleReason>(item, "context.reason", [
      "account_rejected",
      "discovery_failed",
      "not_configured",
      "unknown",
    ]),
  );
  return reason === undefined ? {} : { reason };
}

/**
 * Parse one address that on-call is expected to act on (block, rate-limit).
 *
 * This is the deliberate exception to the "no network identifiers" rule that
 * {@link untrustedString} enforces, and it is safe precisely because it is not
 * a free-text field: a value that is not literally an IP address is rejected,
 * so nothing else can ride along. IPv6 is normalized through URL parsing so a
 * zone id, port, or bracket form cannot slip through as an opaque string.
 */
function sourceAddress(value: unknown, index: number): string {
  const field = `context.source_addresses[${index}]`;
  const raw = stringValue(value, field, 45);
  if (IPV4_STRICT_RE.test(raw)) return raw;
  try {
    const hostname = new URL(`http://[${raw}]/`).hostname;
    // URL keeps IPv6 bracketed; anything else came back changed or empty.
    if (hostname.startsWith("[") && hostname.endsWith("]")) {
      return hostname.slice(1, -1);
    }
  } catch {
    // Fall through to the shared rejection below.
  }
  throw new ValidationError(`${field} must be an IP address`);
}

function sourceAddresses(value: unknown): readonly string[] {
  if (!Array.isArray(value)) {
    throw new ValidationError("context.source_addresses must be an array");
  }
  if (value.length === 0 || value.length > MAX_SOURCE_ADDRESSES) {
    throw new ValidationError(
      `context.source_addresses must contain 1 to ${MAX_SOURCE_ADDRESSES} addresses`,
    );
  }
  const addresses = value.map(sourceAddress);
  if (new Set(addresses).size !== addresses.length) {
    throw new ValidationError("context.source_addresses must not contain duplicates");
  }
  return addresses;
}

function parseMetricThresholdContext(value: unknown): MetricThresholdContext {
  const input = record(value, "context");
  strictKeys(input, METRIC_THRESHOLD_CONTEXT_KEYS, "context");

  // Every field is numeric or a closed enum by design: this type replaces
  // backend alerts that used to embed source IPs, key prefixes, user ids, and
  // pre-formatted rate strings. There is deliberately no free-text field for
  // them to move into.
  const context: {
    metric: BreachedMetric;
    observed: number;
    threshold: number;
    window_sec?: number;
    scope?: BreachScope;
    subject?: string;
    source_addresses?: readonly string[];
    distinct_sources?: number;
    top_source_share?: number;
    sample_count?: number;
  } = {
    metric: enumValue<BreachedMetric>(input.metric, "context.metric", [
      "auth_failure_count",
      "failed_request_rate",
      "http_5xx_rate",
      "latency_p95_ms",
      "prefix_cache_pending_evictions",
      "provider_hourly_spend",
      "tracked_task_failure_rate",
      "user_daily_cost",
    ]),
    observed: boundedNumber(input.observed, "context.observed", 0, 1e12),
    threshold: boundedNumber(input.threshold, "context.threshold", 0, 1e12),
  };
  context.window_sec = optional(input, "window_sec", (item) =>
    boundedInteger(item, "context.window_sec", 1, 31 * 24 * 60 * 60),
  );
  context.scope = optional(input, "scope", (item) =>
    enumValue<BreachScope>(item, "context.scope", [
      "gateway",
      "provider",
      "task",
      "user",
    ]),
  );
  // A bounded identifier, not free text: the provider/task/user on-call acts on.
  context.subject = optional(input, "subject", (item) =>
    stringValue(item, "context.subject", 128, IDENTIFIER_RE),
  );
  context.source_addresses = optional(input, "source_addresses", sourceAddresses);
  context.distinct_sources = optional(input, "distinct_sources", (item) =>
    boundedInteger(item, "context.distinct_sources", 0, 1_000_000_000),
  );
  context.top_source_share = optional(input, "top_source_share", (item) =>
    boundedNumber(item, "context.top_source_share", 0, 1),
  );
  context.sample_count = optional(input, "sample_count", (item) =>
    boundedInteger(item, "context.sample_count", 0, 1_000_000_000),
  );

  for (const key of Object.keys(context) as Array<keyof typeof context>) {
    if (context[key] === undefined) delete context[key];
  }
  return context;
}

function parseDependencyUnavailableContext(
  value: unknown,
): DependencyUnavailableContext {
  const input = record(value, "context");
  strictKeys(input, DEPENDENCY_CONTEXT_KEYS, "context");

  const context: {
    dependency: UnavailableDependency;
    backend?: string;
  } = {
    dependency: enumValue<UnavailableDependency>(
      input.dependency,
      "context.dependency",
      ["log_store", "operational_store"],
    ),
  };
  // An implementation label only ("postgres"), never a connection string —
  // untrustedString additionally rejects hosts, credentials, and IPs.
  context.backend = optional(input, "backend", (item) =>
    untrustedString(item, "context.backend", 64),
  );
  if (context.backend === undefined) delete context.backend;
  return context;
}

function validateCalendarTimestamp(match: RegExpMatchArray): void {
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const zone = match[8];
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    throw new ValidationError("occurred_at must be an ISO timestamp");
  }
  if (zone !== "Z") {
    const offsetHour = Number(zone.slice(1, 3));
    const offsetMinute = Number(zone.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) {
      throw new ValidationError("occurred_at must be an ISO timestamp");
    }
  }

  const calendar = new Date(0);
  calendar.setUTCFullYear(year, month - 1, day);
  calendar.setUTCHours(hour, minute, second, 0);
  if (
    calendar.getUTCFullYear() !== year ||
    calendar.getUTCMonth() !== month - 1 ||
    calendar.getUTCDate() !== day ||
    calendar.getUTCHours() !== hour ||
    calendar.getUTCMinutes() !== minute ||
    calendar.getUTCSeconds() !== second
  ) {
    throw new ValidationError("occurred_at must be an ISO timestamp");
  }
}

function occurredAt(value: unknown, options: ParseAlertOptions): string {
  const raw = stringValue(value, "occurred_at", 64);
  const match = raw.match(RFC3339_RE);
  if (!match) throw new ValidationError("occurred_at must be an ISO timestamp");
  validateCalendarTimestamp(match);
  const timestamp = Date.parse(raw);
  if (!Number.isFinite(timestamp)) {
    throw new ValidationError("occurred_at must be an ISO timestamp");
  }

  const now = options.now instanceof Date ? options.now.getTime() : (options.now ?? Date.now());
  const skew = options.maxFutureSkewMs ?? DEFAULT_MAX_FUTURE_SKEW_MS;
  if (!Number.isFinite(now) || !Number.isFinite(skew) || skew < 0) {
    throw new ValidationError("timestamp validation options are invalid");
  }
  if (timestamp > now + skew) {
    throw new ValidationError("occurred_at is too far in the future");
  }
  return new Date(timestamp).toISOString();
}

function evidenceReference(value: unknown, index: number): string {
  const field = `evidence_refs[${index}]`;
  const reference = untrustedString(value, field, 500);
  if (
    reference.startsWith("/") ||
    reference.startsWith("~") ||
    reference.includes("\\") ||
    reference.includes("?") ||
    reference.includes("#") ||
    /^[A-Za-z][A-Za-z0-9+.-]*:/.test(reference) ||
    /%(?:2e|2f|5c)/i.test(reference)
  ) {
    throw new ValidationError(`${field} must be a repository-relative path`);
  }
  const segments = reference.split("/");
  if (segments.some((segment) => !segment || segment === "." || segment === "..")) {
    throw new ValidationError(`${field} must not contain path traversal`);
  }
  return reference;
}

function evidenceReferences(value: unknown): readonly string[] {
  if (!Array.isArray(value)) {
    throw new ValidationError("evidence_refs must be an array");
  }
  if (value.length > 20) {
    throw new ValidationError("evidence_refs must contain at most 20 paths");
  }
  const references = value.map(evidenceReference);
  if (new Set(references).size !== references.length) {
    throw new ValidationError("evidence_refs must not contain duplicates");
  }
  return references;
}

/** Parse the sole producer wire contract and reject every non-canonical field. */
export function parseAlertEvent(value: unknown, options: ParseAlertOptions = {}): AlertEvent {
  const input = record(value, "alert");
  for (const key of Object.keys(input)) {
    if (PRODUCER_FORBIDDEN_KEYS.has(key)) {
      throw new ValidationError(`alert contains trusted or Slack-owned field: ${key}`);
    }
  }
  strictKeys(input, EVENT_KEYS, "alert");
  if (input.schema_version !== 1) {
    throw new ValidationError("schema_version must be 1");
  }
  if (
    input.alert_type !== "provider_circuit_open" &&
    input.alert_type !== "model_unavailable" &&
    input.alert_type !== "monitoring_cycle_failure" &&
    input.alert_type !== "metric_threshold_breach" &&
    input.alert_type !== "dependency_unavailable"
  ) {
    throw new ValidationError("alert_type is unsupported");
  }

  const base = {
    schema_version: 1 as const,
    event_id: stringValue(input.event_id, "event_id", 128, IDENTIFIER_RE),
    fingerprint: untrustedString(input.fingerprint, "fingerprint", 512),
    status: enumValue<AlertStatus>(input.status, "status", ["firing", "resolved"]),
    severity: enumValue<AlertSeverity>(input.severity, "severity", [
      "critical",
      "error",
      "warn",
      "info",
    ]),
    title: untrustedString(input.title, "title", 500),
    occurred_at: occurredAt(input.occurred_at, options),
    summary: untrustedString(input.summary, "summary", 4_000),
    evidence_refs: evidenceReferences(input.evidence_refs),
  };
  if (input.alert_type === "model_unavailable") {
    const context = parseModelUnavailableContext(input.context);
    if (
      base.status === "firing" &&
      (context.consecutive_failures === undefined ||
        context.failure_threshold === undefined ||
        context.reason === undefined)
    ) {
      throw new ValidationError(
        "firing model_unavailable context requires consecutive_failures, failure_threshold, and reason",
      );
    }
    if (
      base.status === "resolved" &&
      (context.consecutive_failures !== undefined ||
        context.failure_threshold !== undefined ||
        context.reason !== undefined)
    ) {
      throw new ValidationError(
        "resolved model_unavailable context must not contain firing-only fields",
      );
    }
    return {
      ...base,
      alert_type: "model_unavailable",
      context,
    };
  }
  if (input.alert_type === "monitoring_cycle_failure") {
    const context = parseMonitoringCycleContext(input.context);
    if (base.status === "firing" && context.reason === undefined) {
      throw new ValidationError(
        "firing monitoring_cycle_failure context requires reason",
      );
    }
    if (base.status === "resolved" && context.reason !== undefined) {
      throw new ValidationError(
        "resolved monitoring_cycle_failure context must not contain firing-only fields",
      );
    }
    return {
      ...base,
      alert_type: "monitoring_cycle_failure",
      context,
    };
  }
  if (input.alert_type === "metric_threshold_breach") {
    return {
      ...base,
      alert_type: "metric_threshold_breach",
      context: parseMetricThresholdContext(input.context),
    };
  }
  if (input.alert_type === "dependency_unavailable") {
    return {
      ...base,
      alert_type: "dependency_unavailable",
      context: parseDependencyUnavailableContext(input.context),
    };
  }
  return {
    ...base,
    alert_type: "provider_circuit_open",
    context: parseProviderCircuitContext(input.context),
  };
}

/** Validate identity metadata after authentication and registry lookup. */
export function parseTrustedMetadata(value: unknown): TrustedAlertMetadata {
  const input = record(value, "trusted metadata");
  strictKeys(input, TRUSTED_METADATA_KEYS, "trusted metadata");
  const deploymentSha = stringValue(input.deployment_sha, "deployment_sha", 40, FULL_SHA_RE);
  const artifactDigest = stringValue(
    input.artifact_digest,
    "artifact_digest",
    71,
    ARTIFACT_DIGEST_RE,
  );
  return {
    environment: enumValue<TrustedEnvironment>(input.environment, "environment", [
      "staging",
      "production",
    ]),
    source: enumValue<TrustedSource>(input.source, "source", ["gateway", "status-monitor"]),
    principal: stringValue(input.principal, "principal", 128, IDENTIFIER_RE),
    deployment_id: stringValue(input.deployment_id, "deployment_id", 128, IDENTIFIER_RE),
    deployment_sha: deploymentSha.toLowerCase(),
    artifact_digest: artifactDigest.toLowerCase(),
    registry_version: boundedInteger(
      input.registry_version,
      "registry_version",
      1,
      Number.MAX_SAFE_INTEGER,
    ),
  };
}

export function createCanonicalEnvelope(
  event: unknown,
  trusted: unknown,
  options: ParseAlertOptions = {},
): CanonicalAlertEnvelope {
  return {
    event: parseAlertEvent(event, options),
    trusted: parseTrustedMetadata(trusted),
  };
}

/** RFC-8785-style stable JSON for the contract's JSON-safe value subset. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new ValidationError("canonical JSON contains a non-finite number");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  if (typeof value === "object") {
    const object = value as Record<string, unknown>;
    const keys = Object.keys(object).sort();
    return `{${keys
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
      .join(",")}}`;
  }
  throw new ValidationError("canonical JSON contains an unsupported value");
}

/** Stable SHA-256 digest over the canonical JSON of an arbitrary contract value. */
export async function canonicalDigest(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const buffer = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(buffer).set(bytes);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", buffer));
  return `sha256:${Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

/** Digest used for same-shard event_id conflict detection. */
export async function canonicalEventDigest(event: AlertEvent): Promise<string> {
  return canonicalDigest(event);
}
