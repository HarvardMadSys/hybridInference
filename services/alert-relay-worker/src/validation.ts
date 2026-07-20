import type {
  AlertEventV2,
  AlertSeverity,
  AlertStatus,
  FailedCompletion,
  JobCompletion,
  JsonValue,
  OnCallAnalysis,
  SuccessfulCompletion,
} from "./types";

const EVENT_KEYS = new Set([
  "version",
  "alert_id",
  "fingerprint",
  "source",
  "status",
  "severity",
  "title",
  "occurred_at",
  "summary",
  "context",
  "deployment_sha",
  "evidence_refs",
]);
const SECRET_KEY_PARTS = ["api_key", "apikey", "authorization", "cookie", "password", "secret", "token"];
const SENSITIVE_CONTEXT_KEYS = new Set([
  "email",
  "key_prefix",
  "offending_users",
  "remote_ip",
  "top_ips",
  "top_key_prefixes",
  "user_id",
  "user_name",
]);
const BEARER_RE = /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi;
const KEY_VALUE_RE = /\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+/gi;
const HYBRID_KEY_RE = /\bhyi-[A-Za-z0-9_-]{20,}\b/g;
const SINGLE_LINE_CONTROL_RE = /[\u0000-\u001f\u007f-\u009f]/g;
const MULTILINE_CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g;

export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

function record(value: unknown, field = "body"): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ValidationError(`${field} must be an object`);
  }
  return value as Record<string, unknown>;
}

function strictKeys(value: Record<string, unknown>, allowed: Set<string>, field: string): void {
  const extra = Object.keys(value).filter((key) => !allowed.has(key));
  if (extra.length > 0) {
    throw new ValidationError(`${field} contains unsupported field: ${extra[0]}`);
  }
}

function text(
  value: unknown,
  field: string,
  maxLength: number,
  options: { multiline?: boolean; pattern?: RegExp } = {},
): string {
  if (typeof value !== "string") throw new ValidationError(`${field} must be a string`);
  const cleaned = value
    .replace(options.multiline ? MULTILINE_CONTROL_RE : SINGLE_LINE_CONTROL_RE, " ")
    .trim();
  if (!cleaned) throw new ValidationError(`${field} must not be blank`);
  if (cleaned.length > maxLength) {
    throw new ValidationError(`${field} must be at most ${maxLength} characters`);
  }
  if (options.pattern && !options.pattern.test(cleaned)) {
    throw new ValidationError(`${field} has an invalid format`);
  }
  return cleaned;
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

function redactString(value: string): string {
  return value
    .replace(BEARER_RE, "Bearer [REDACTED]")
    .replace(KEY_VALUE_RE, (_match, key: string) => `${key}=[REDACTED]`)
    .replace(HYBRID_KEY_RE, "[REDACTED]");
}

function content(value: unknown, field: string, maxLength: number): string {
  return redactString(text(value, field, maxLength, { multiline: true }));
}

function sanitizeJson(value: unknown, key = "", depth = 0): JsonValue {
  const normalizedKey = key.toLowerCase().replaceAll("-", "_");
  if (
    SENSITIVE_CONTEXT_KEYS.has(normalizedKey) ||
    SECRET_KEY_PARTS.some((part) => normalizedKey.includes(part))
  ) {
    return "[REDACTED]";
  }
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new ValidationError("context contains a non-finite number");
    return value;
  }
  if (typeof value === "string") {
    const cleaned = value.replace(MULTILINE_CONTROL_RE, " ").trim();
    if (cleaned.length > 2_000) {
      throw new ValidationError("context strings must be at most 2000 characters");
    }
    return redactString(cleaned);
  }
  if (depth >= 5) throw new ValidationError("context exceeds maximum depth");
  if (Array.isArray(value)) {
    if (value.length > 50) throw new ValidationError("context arrays may contain at most 50 items");
    return value.map((item) => sanitizeJson(item, "", depth + 1));
  }
  const object = record(value, "context value");
  const entries = Object.entries(object);
  if (entries.length > 50) throw new ValidationError("context objects may contain at most 50 keys");
  const result: Record<string, JsonValue> = Object.create(null) as Record<string, JsonValue>;
  for (const [childKey, childValue] of entries) {
    const safeKey = text(childKey, "context key", 128);
    result[safeKey] = sanitizeJson(childValue, safeKey, depth + 1);
  }
  return result;
}

function context(value: unknown): Record<string, JsonValue> {
  const sanitized = sanitizeJson(record(value, "context"));
  if (sanitized === null || Array.isArray(sanitized) || typeof sanitized !== "object") {
    throw new ValidationError("context must be an object");
  }
  if (JSON.stringify(sanitized).length > 32_000) {
    throw new ValidationError("context is too large");
  }
  return sanitized;
}

function occurredAt(value: unknown): string {
  const raw = text(value, "occurred_at", 64);
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.test(raw)
  ) {
    throw new ValidationError("occurred_at must be an ISO timestamp");
  }
  const timestamp = Date.parse(raw);
  if (!Number.isFinite(timestamp)) throw new ValidationError("occurred_at must be an ISO timestamp");
  return new Date(timestamp).toISOString();
}

export function parseAlertEvent(value: unknown): AlertEventV2 {
  const input = record(value);
  strictKeys(input, EVENT_KEYS, "alert");
  if (input.version !== "2") throw new ValidationError('version must be "2"');

  const event: AlertEventV2 = {
    version: "2",
    alert_id: text(input.alert_id, "alert_id", 128, { pattern: /^[A-Za-z0-9._:-]+$/ }),
    fingerprint: text(input.fingerprint, "fingerprint", 512),
    source: text(input.source, "source", 128, { pattern: /^[A-Za-z0-9._:-]+$/ }),
    status: enumValue<AlertStatus>(input.status, "status", ["firing", "resolved"]),
    severity: enumValue<AlertSeverity>(input.severity, "severity", [
      "critical",
      "error",
      "warn",
      "info",
    ]),
    title: content(input.title, "title", 500),
    occurred_at: occurredAt(input.occurred_at),
    summary: content(input.summary, "summary", 4_000),
    context: context(input.context),
  };
  if (input.deployment_sha !== undefined) {
    event.deployment_sha = text(input.deployment_sha, "deployment_sha", 40, {
      pattern: /^[A-Fa-f0-9]{40}$/,
    });
  }
  if (input.evidence_refs !== undefined) {
    if (!Array.isArray(input.evidence_refs) || input.evidence_refs.length > 20) {
      throw new ValidationError("evidence_refs must contain at most 20 strings");
    }
    event.evidence_refs = input.evidence_refs.map((item, index) =>
      content(item, `evidence_refs[${index}]`, 500),
    );
  }
  return event;
}

const ANALYSIS_KEYS = new Set([
  "summary",
  "classification",
  "confidence",
  "impact",
  "evidence",
  "likely_cause",
  "recommended_actions",
  "issue_recommendation",
  "draft_pr_recommendation",
]);

function stringList(
  value: unknown,
  field: string,
  minItems: number,
  maxItems: number,
): string[] {
  if (!Array.isArray(value) || value.length < minItems || value.length > maxItems) {
    throw new ValidationError(`${field} must contain ${minItems}-${maxItems} strings`);
  }
  return value.map((item, index) =>
    content(item, `${field}[${index}]`, 1_000),
  );
}

export function parseAnalysis(value: unknown): OnCallAnalysis {
  const input = record(value, "analysis");
  strictKeys(input, ANALYSIS_KEYS, "analysis");
  if (typeof input.confidence !== "number" || input.confidence < 0 || input.confidence > 1) {
    throw new ValidationError("analysis.confidence must be between 0 and 1");
  }
  return {
    summary: content(input.summary, "analysis.summary", 4_000),
    classification: enumValue(input.classification, "analysis.classification", [
      "code_bug",
      "upstream_provider",
      "configuration",
      "capacity",
      "authentication",
      "unknown",
    ]),
    confidence: input.confidence,
    impact: content(input.impact, "analysis.impact", 4_000),
    evidence: stringList(input.evidence, "analysis.evidence", 0, 10),
    likely_cause: content(input.likely_cause, "analysis.likely_cause", 4_000),
    recommended_actions: stringList(
      input.recommended_actions,
      "analysis.recommended_actions",
      1,
      8,
    ),
    issue_recommendation: enumValue(
      input.issue_recommendation,
      "analysis.issue_recommendation",
      ["none", "create"],
    ),
    draft_pr_recommendation: enumValue(
      input.draft_pr_recommendation,
      "analysis.draft_pr_recommendation",
      ["none", "create"],
    ),
  };
}

export function parseCompletion(value: unknown): JobCompletion {
  const input = record(value);
  if (input.status === "success") {
    strictKeys(input, new Set(["status", "analysis", "codex_thread_id"]), "completion");
    const completion: SuccessfulCompletion = {
      status: "success",
      analysis: parseAnalysis(input.analysis),
    };
    if (input.codex_thread_id !== undefined) {
      completion.codex_thread_id = text(input.codex_thread_id, "codex_thread_id", 128);
    }
    return completion;
  }
  if (input.status === "failure") {
    strictKeys(input, new Set(["status", "error", "run_url"]), "completion");
    const runUrl = text(input.run_url, "run_url", 2_000);
    let parsed: URL;
    try {
      parsed = new URL(runUrl);
    } catch {
      throw new ValidationError("run_url must be a valid HTTPS URL");
    }
    if (parsed.protocol !== "https:" || !parsed.hostname || parsed.username || parsed.password) {
      throw new ValidationError("run_url must be a valid HTTPS URL");
    }
    const completion: FailedCompletion = {
      status: "failure",
      error: content(input.error, "error", 2_000),
      run_url: parsed.toString(),
    };
    return completion;
  }
  throw new ValidationError('completion status must be "success" or "failure"');
}
