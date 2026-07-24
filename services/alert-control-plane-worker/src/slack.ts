import {
  type DeliveryRef,
  NOTIFICATION_ACTION_TYPES,
  type NotificationAction,
  type NotificationActionResult,
  type NotificationAttemptMode,
  type NotificationSink,
  parseDeliveryRef,
} from "./notification";
import {
  type IncidentRenderState,
  renderAnalysisReply,
  renderParent,
  renderRecoveryReply,
} from "./render";
import type { CanonicalAlertEnvelope, SlackMessage } from "./types";

const SLACK_API_ROOT = "https://slack.com/api/";
const DEFAULT_RECONCILIATION_GRACE_MS = 30_000;
const DEFAULT_WINDOW_BEFORE_MS = 5_000;
const DEFAULT_WINDOW_AFTER_MS = 120_000;
const DEFAULT_MAX_PAGES = 100;
const DEFAULT_REQUEST_TIMEOUT_MS = 10_000;
// Current Slack limits reduce history/replies pages to 15 for some app classes.
const PAGE_LIMIT = 15;
const CHANNEL_ID_RE = /^[CGD][A-Z0-9]{1,255}$/;
const SHA256_RE = /^sha256:[0-9a-f]{64}$/;

const PERMANENT_SLACK_ERRORS: ReadonlySet<string> = new Set([
  "access_denied",
  "account_inactive",
  "app_access_restricted",
  "cannot_reply_to_message",
  "cant_update_message",
  "channel_not_found",
  "deprecated_endpoint",
  "ekm_access_denied",
  "enterprise_is_restricted",
  "invalid_arg_name",
  "invalid_arguments",
  "invalid_array_arg",
  "invalid_auth",
  "invalid_blocks",
  "invalid_blocks_format",
  "invalid_charset",
  "invalid_form_data",
  "invalid_metadata_format",
  "invalid_metadata_schema",
  "invalid_post_type",
  "is_archived",
  "metadata_must_be_sent_from_app",
  "metadata_too_large",
  "method_deprecated",
  "missing_post_type",
  "missing_scope",
  "no_permission",
  "no_text",
  "not_allowed_token_type",
  "not_authed",
  "not_in_channel",
  "restricted_action",
  "restricted_action_read_only_channel",
  "restricted_action_thread_locked",
  "restricted_action_thread_only_channel",
  "team_access_not_granted",
  "team_not_found",
  "token_expired",
  "token_revoked",
  "too_many_attachments",
]);

const AUTHORIZATION_SLACK_ERRORS: ReadonlySet<string> = new Set([
  "access_denied",
  "account_inactive",
  "app_access_restricted",
  "ekm_access_denied",
  "invalid_auth",
  "missing_scope",
  "no_permission",
  "not_allowed_token_type",
  "not_authed",
  "team_access_not_granted",
  "team_not_found",
  "token_expired",
  "token_revoked",
]);

const CONFIGURATION_SLACK_ERRORS: ReadonlySet<string> = new Set([
  "cannot_reply_to_message",
  "cant_update_message",
  "channel_not_found",
  "enterprise_is_restricted",
  "is_archived",
  "metadata_must_be_sent_from_app",
  "not_in_channel",
  "restricted_action",
  "restricted_action_read_only_channel",
  "restricted_action_thread_locked",
  "restricted_action_thread_only_channel",
]);

const RATE_LIMIT_ERRORS: ReadonlySet<string> = new Set(["ratelimited", "rate_limited"]);

export interface SlackSinkOptions {
  readonly sinkId: string;
  readonly botToken: string;
  readonly channelId: string;
  readonly fetch?: typeof fetch;
  readonly now?: () => number;
  readonly reconciliationGraceMs?: number;
  readonly reconciliationWindowBeforeMs?: number;
  readonly reconciliationWindowAfterMs?: number;
  readonly maxPages?: number;
  /** Must stay below the outbox claim lease so a hung call cannot be reclaimed in flight. */
  readonly requestTimeoutMs?: number;
}

interface SlackApiResponse {
  readonly ok?: unknown;
  readonly error?: unknown;
  readonly channel?: unknown;
  readonly ts?: unknown;
  readonly messages?: unknown;
  readonly has_more?: unknown;
  readonly is_limited?: unknown;
  readonly response_metadata?: unknown;
}

interface SlackMessageRecord {
  readonly ts: string;
  readonly threadTs?: string;
  readonly metadata?: unknown;
}

type QueryResult =
  | {
      readonly outcome: "success";
      readonly messages: readonly SlackMessageRecord[];
      readonly retentionLimited: boolean;
    }
  | { readonly outcome: "uncertain"; readonly reconcileAtMs?: number }
  | { readonly outcome: "incomplete" };

type HttpResult =
  | {
      readonly outcome: "response";
      readonly response: Response;
      readonly parsed?: SlackApiResponse;
    }
  | { readonly outcome: "network_error" };

class LocalActionError extends Error {}

function record(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new LocalActionError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function requiredString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new LocalActionError(`${label} must be a non-empty string`);
  }
  return value;
}

function finiteInteger(value: unknown, label: string, minimum: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
    throw new LocalActionError(`${label} must be an integer at least ${minimum}`);
  }
  return value;
}

function finiteNonNegative(value: number, fallback: number): number {
  return Number.isFinite(value) && value >= 0 ? value : fallback;
}

function positiveInteger(value: number, fallback: number): number {
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function parseEnvelope(value: unknown): CanonicalAlertEnvelope {
  const envelope = record(value, "notification envelope");
  const event = record(envelope.event, "notification event");
  record(envelope.trusted, "trusted notification metadata");
  requiredString(event.occurred_at, "notification event occurred_at");
  return envelope as unknown as CanonicalAlertEnvelope;
}

function optionalPayloadString(
  payload: Readonly<Record<string, unknown>>,
  key: string,
  fallback: string,
): string {
  const value = payload[key];
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function renderState(
  action: NotificationAction,
  envelope: CanonicalAlertEnvelope | null,
): IncidentRenderState {
  const fallbackTime =
    envelope?.event.occurred_at ?? new Date(action.attemptStartedAtMs).toISOString();
  const occurrenceCount = action.payload.occurrence_count;
  if (action.type !== "post_analysis") {
    finiteInteger(occurrenceCount, "occurrence_count", 1);
    requiredString(action.payload.first_seen, "first_seen");
    requiredString(action.payload.last_seen, "last_seen");
  }
  return {
    action_id: action.actionId,
    incident_id: action.incidentId,
    generation: action.generation,
    payload_digest: action.payloadDigest,
    occurrence_count:
      typeof occurrenceCount === "number" &&
      Number.isSafeInteger(occurrenceCount) &&
      occurrenceCount > 0
        ? occurrenceCount
        : 1,
    first_seen: optionalPayloadString(action.payload, "first_seen", fallbackTime),
    last_seen: optionalPayloadString(action.payload, "last_seen", fallbackTime),
  };
}

function renderAction(action: NotificationAction): SlackMessage {
  if (action.type === "post_analysis") {
    const analysis = record(action.payload.analysis, "analysis");
    return renderAnalysisReply(analysis, renderState(action, null));
  }
  const envelope = parseEnvelope(action.payload.envelope);
  const state = renderState(action, envelope);
  return action.type === "post_recovery"
    ? renderRecoveryReply(envelope, state)
    : renderParent(envelope, state);
}

function slackTimestampMs(value: string): number | null {
  if (!/^\d{1,16}\.\d{1,9}$/.test(value)) return null;
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return null;
  return seconds * 1_000;
}

function seconds(valueMs: number): string {
  return (valueMs / 1_000).toFixed(3);
}

function safeRetryAfterMs(response: Response, nowMs: number): number | undefined {
  const raw = response.headers.get("retry-after");
  if (raw === null) return undefined;
  const delaySeconds = Number(raw);
  if (!Number.isFinite(delaySeconds) || delaySeconds < 0) return undefined;
  return nowMs + Math.ceil(delaySeconds * 1_000);
}

function responseCursor(response: SlackApiResponse): string {
  const metadata = response.response_metadata;
  if (metadata === null || typeof metadata !== "object" || Array.isArray(metadata)) return "";
  const cursor = (metadata as Record<string, unknown>).next_cursor;
  return typeof cursor === "string" ? cursor.trim() : "";
}

function parseMessages(value: unknown): readonly SlackMessageRecord[] | null {
  if (!Array.isArray(value)) return null;
  const messages: SlackMessageRecord[] = [];
  for (const item of value) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const input = item as Record<string, unknown>;
    if (typeof input.ts !== "string") return null;
    messages.push({
      ts: input.ts,
      ...(typeof input.thread_ts === "string" ? { threadTs: input.thread_ts } : {}),
      ...(Object.hasOwn(input, "metadata") ? { metadata: input.metadata } : {}),
    });
  }
  return messages;
}

function metadataComparison(
  message: SlackMessageRecord,
  action: NotificationAction,
): "match" | "conflict" | "unrelated" {
  if (
    message.metadata === null ||
    typeof message.metadata !== "object" ||
    Array.isArray(message.metadata)
  ) {
    return "unrelated";
  }
  const metadata = message.metadata as Record<string, unknown>;
  if (metadata.event_type !== "alert_control_plane_action") return "unrelated";
  const rawPayload = metadata.event_payload;
  if (rawPayload === null || typeof rawPayload !== "object" || Array.isArray(rawPayload)) {
    return "conflict";
  }
  const payload = rawPayload as Record<string, unknown>;
  if (typeof payload.action_id !== "string") return "conflict";
  if (payload.action_id !== action.actionId) return "unrelated";
  if (
    payload.incident_id !== action.incidentId ||
    payload.generation !== action.generation ||
    payload.payload_digest !== action.payloadDigest
  ) {
    return "conflict";
  }
  return "match";
}

/**
 * Slack implementation of the platform-neutral notification boundary.
 *
 * This class never throws from `execute` and never exposes response bodies or
 * credentials in results. It is deliberately not wired into the Worker runtime
 * until the target workspace readback gate has been exercised.
 */
export class SlackSink implements NotificationSink {
  readonly platform = "slack" as const;
  readonly sinkId: string;
  private readonly botToken: string;
  private readonly channelId: string;
  private readonly fetchImpl: typeof fetch;
  private readonly now: () => number;
  private readonly reconciliationGraceMs: number;
  private readonly windowBeforeMs: number;
  private readonly windowAfterMs: number;
  private readonly maxPages: number;
  private readonly requestTimeoutMs: number;

  constructor(options: SlackSinkOptions) {
    this.sinkId = options.sinkId;
    this.botToken = options.botToken;
    this.channelId = options.channelId;
    this.fetchImpl = options.fetch ?? fetch;
    this.now = options.now ?? Date.now;
    this.reconciliationGraceMs = finiteNonNegative(
      options.reconciliationGraceMs ?? DEFAULT_RECONCILIATION_GRACE_MS,
      DEFAULT_RECONCILIATION_GRACE_MS,
    );
    this.windowBeforeMs = finiteNonNegative(
      options.reconciliationWindowBeforeMs ?? DEFAULT_WINDOW_BEFORE_MS,
      DEFAULT_WINDOW_BEFORE_MS,
    );
    this.windowAfterMs = finiteNonNegative(
      options.reconciliationWindowAfterMs ?? DEFAULT_WINDOW_AFTER_MS,
      DEFAULT_WINDOW_AFTER_MS,
    );
    this.maxPages = positiveInteger(options.maxPages ?? DEFAULT_MAX_PAGES, DEFAULT_MAX_PAGES);
    this.requestTimeoutMs = positiveInteger(
      options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
      DEFAULT_REQUEST_TIMEOUT_MS,
    );
  }

  async execute(
    action: NotificationAction,
    mode: NotificationAttemptMode,
  ): Promise<NotificationActionResult> {
    try {
      this.validateConfig();
      this.validateAction(action);
    } catch {
      return { outcome: "failed", errorCode: "slack_action_invalid" };
    }

    if (mode === "reconcile") return this.reconcile(action);

    let message: SlackMessage;
    try {
      message = renderAction(action);
    } catch {
      return { outcome: "failed", errorCode: "slack_payload_invalid" };
    }
    return this.write(action, message);
  }

  private validateConfig(): void {
    requiredString(this.sinkId, "Slack sinkId");
    requiredString(this.botToken, "Slack bot token");
    if (!CHANNEL_ID_RE.test(this.channelId)) {
      throw new LocalActionError("Slack channelId is invalid");
    }
  }

  private validateAction(action: NotificationAction): void {
    if (!(NOTIFICATION_ACTION_TYPES as readonly string[]).includes(action.type)) {
      throw new LocalActionError("notification action type is unsupported");
    }
    if (action.sinkId !== this.sinkId) throw new LocalActionError("Slack sinkId mismatch");
    requiredString(action.actionId, "actionId");
    requiredString(action.incidentId, "incidentId");
    finiteInteger(action.generation, "generation", 1);
    if (!Number.isFinite(action.attemptStartedAtMs) || action.attemptStartedAtMs < 0) {
      throw new LocalActionError("attemptStartedAtMs is invalid");
    }
    if (!SHA256_RE.test(action.payloadDigest)) {
      throw new LocalActionError("payloadDigest is invalid");
    }
    record(action.payload, "notification payload");

    if (action.type === "post_parent") {
      if (action.deliveryRef !== null) throw new LocalActionError("parent already has a reference");
      return;
    }
    const ref = parseDeliveryRef(action.deliveryRef);
    if (
      ref.sinkId !== this.sinkId ||
      ref.platform !== this.platform ||
      !CHANNEL_ID_RE.test(ref.destinationId) ||
      slackTimestampMs(ref.messageId) === null ||
      (ref.conversationId !== undefined && slackTimestampMs(ref.conversationId) === null)
    ) {
      throw new LocalActionError("delivery reference does not belong to this Slack sink");
    }
  }

  private async write(
    action: NotificationAction,
    message: SlackMessage,
  ): Promise<NotificationActionResult> {
    const ref = action.deliveryRef;
    let method: "chat.postMessage" | "chat.update";
    let body: Record<string, unknown>;
    if (action.type === "post_parent") {
      method = "chat.postMessage";
      body = { channel: this.channelId, ...message, unfurl_links: false, unfurl_media: false };
    } else if (action.type === "update_parent") {
      method = "chat.update";
      body = {
        channel: ref!.destinationId,
        ts: ref!.messageId,
        as_user: true,
        ...message,
      };
    } else {
      method = "chat.postMessage";
      body = {
        channel: ref!.destinationId,
        thread_ts: ref!.conversationId ?? ref!.messageId,
        ...message,
        unfurl_links: false,
        unfurl_media: false,
      };
    }

    const transport = await this.requestJson(`${SLACK_API_ROOT}${method}`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${this.botToken}`,
        "content-type": "application/json; charset=utf-8",
      },
      body: JSON.stringify(body),
    });
    if (transport.outcome === "network_error") {
      return { outcome: "uncertain", errorCode: "slack_network_uncertain" };
    }
    const { response, parsed } = transport;

    if (response.status === 429) {
      return {
        outcome: "retry",
        errorCode: "slack_rate_limited",
        retryAtMs: safeRetryAfterMs(response, this.now()),
      };
    }
    if (response.status >= 500) {
      return { outcome: "uncertain", errorCode: "slack_server_uncertain" };
    }
    if (parsed === undefined) {
      return { outcome: "uncertain", errorCode: "slack_response_uncertain" };
    }
    if (!response.ok || parsed.ok !== true) return this.classifySlackError(parsed, response);
    if (typeof parsed.ts !== "string" || slackTimestampMs(parsed.ts) === null) {
      return { outcome: "uncertain", errorCode: "slack_response_uncertain" };
    }
    const expectedChannel = ref?.destinationId ?? this.channelId;
    if (typeof parsed.channel === "string" && parsed.channel !== expectedChannel) {
      return {
        outcome: "manual_reconciliation_required",
        errorCode: "slack_response_reference_mismatch",
      };
    }

    if (action.type === "post_parent") {
      const deliveryRef: DeliveryRef = {
        schemaVersion: 1,
        sinkId: this.sinkId,
        platform: this.platform,
        destinationId: this.channelId,
        messageId: parsed.ts,
        conversationId: parsed.ts,
      };
      return {
        outcome: "success",
        receipt: { deliveryRef, externalEffectId: parsed.ts },
      };
    }
    if (action.type === "update_parent" && parsed.ts !== ref!.messageId) {
      return {
        outcome: "manual_reconciliation_required",
        errorCode: "slack_response_reference_mismatch",
      };
    }
    if (
      (action.type === "post_recovery" || action.type === "post_analysis") &&
      parsed.ts === (ref!.conversationId ?? ref!.messageId)
    ) {
      return {
        outcome: "manual_reconciliation_required",
        errorCode: "slack_response_reference_mismatch",
      };
    }
    return {
      outcome: "success",
      receipt: { deliveryRef: ref!, externalEffectId: parsed.ts },
    };
  }

  private classifySlackError(
    parsed: SlackApiResponse,
    response: Response,
  ): NotificationActionResult {
    const error = typeof parsed.error === "string" ? parsed.error : "";
    if (RATE_LIMIT_ERRORS.has(error)) {
      return {
        outcome: "retry",
        errorCode: "slack_rate_limited",
        retryAtMs: safeRetryAfterMs(response, this.now()),
      };
    }
    if (PERMANENT_SLACK_ERRORS.has(error)) {
      if (AUTHORIZATION_SLACK_ERRORS.has(error)) {
        return { outcome: "failed", errorCode: "slack_authorization_rejected" };
      }
      if (CONFIGURATION_SLACK_ERRORS.has(error)) {
        return { outcome: "failed", errorCode: "slack_configuration_rejected" };
      }
      return { outcome: "failed", errorCode: "slack_request_invalid" };
    }
    // Slack documents internal_error/fatal_error as potentially partially
    // successful. Unknown errors receive the same conservative treatment.
    return { outcome: "uncertain", errorCode: "slack_api_uncertain" };
  }

  private async reconcile(action: NotificationAction): Promise<NotificationActionResult> {
    const ref = action.deliveryRef;
    const lowerMs = action.attemptStartedAtMs - this.windowBeforeMs;
    const upperMs = action.attemptStartedAtMs + this.windowAfterMs;
    let method: "conversations.history" | "conversations.replies";
    let params: Record<string, string>;

    if (action.type === "post_parent") {
      method = "conversations.history";
      params = {
        channel: this.channelId,
        oldest: seconds(Math.max(0, lowerMs)),
        latest: seconds(upperMs),
        inclusive: "true",
        limit: String(PAGE_LIMIT),
        include_all_metadata: "true",
      };
    } else if (action.type === "update_parent") {
      method = "conversations.history";
      params = {
        channel: ref!.destinationId,
        oldest: ref!.messageId,
        latest: ref!.messageId,
        inclusive: "true",
        limit: "1",
        include_all_metadata: "true",
      };
    } else {
      method = "conversations.replies";
      params = {
        channel: ref!.destinationId,
        ts: ref!.conversationId ?? ref!.messageId,
        oldest: seconds(Math.max(0, lowerMs)),
        latest: seconds(upperMs),
        inclusive: "true",
        limit: String(PAGE_LIMIT),
        include_all_metadata: "true",
      };
    }

    const query = await this.queryAll(method, params);
    if (query.outcome === "uncertain") {
      return {
        outcome: "uncertain",
        errorCode: "slack_reconcile_uncertain",
        reconcileAtMs: query.reconcileAtMs,
      };
    }
    if (query.outcome === "incomplete") {
      return {
        outcome: "manual_reconciliation_required",
        errorCode: "slack_reconcile_incomplete",
      };
    }

    const candidates = query.messages.filter((message) =>
      this.messageIsInScope(message, action, lowerMs, upperMs),
    );
    if (candidates.some((message) => metadataComparison(message, action) === "conflict")) {
      return {
        outcome: "manual_reconciliation_required",
        errorCode: "slack_reconcile_metadata_conflict",
      };
    }
    const matches = candidates.filter(
      (message) => metadataComparison(message, action) === "match",
    );
    if (matches.length > 1) {
      return {
        outcome: "manual_reconciliation_required",
        errorCode: "slack_reconcile_multiple_matches",
      };
    }
    if (matches.length === 0) {
      if (
        action.type === "update_parent" &&
        query.retentionLimited &&
        candidates.length === 0
      ) {
        return {
          outcome: "manual_reconciliation_required",
          errorCode: "slack_reconcile_incomplete",
        };
      }
      const graceEndsAtMs = action.attemptStartedAtMs + this.reconciliationGraceMs;
      if (this.now() < graceEndsAtMs) {
        return {
          outcome: "uncertain",
          errorCode: "slack_reconcile_visibility_pending",
          reconcileAtMs: graceEndsAtMs,
        };
      }
      return { outcome: "retry", errorCode: "slack_effect_absent" };
    }

    const effect = matches[0];
    if (action.type === "post_parent") {
      const deliveryRef: DeliveryRef = {
        schemaVersion: 1,
        sinkId: this.sinkId,
        platform: this.platform,
        destinationId: this.channelId,
        messageId: effect.ts,
        conversationId: effect.ts,
      };
      return {
        outcome: "success",
        receipt: { deliveryRef, externalEffectId: effect.ts },
      };
    }
    if (action.type === "update_parent" && effect.ts !== ref!.messageId) {
      return {
        outcome: "manual_reconciliation_required",
        errorCode: "slack_reconcile_reference_mismatch",
      };
    }
    return {
      outcome: "success",
      receipt: { deliveryRef: ref!, externalEffectId: effect.ts },
    };
  }

  private messageIsInScope(
    message: SlackMessageRecord,
    action: NotificationAction,
    lowerMs: number,
    upperMs: number,
  ): boolean {
    if (action.type === "update_parent") return message.ts === action.deliveryRef!.messageId;
    const timestampMs = slackTimestampMs(message.ts);
    if (timestampMs === null || timestampMs < lowerMs || timestampMs > upperMs) return false;
    if (action.type === "post_parent") return message.threadTs === undefined;
    const root = action.deliveryRef!.conversationId ?? action.deliveryRef!.messageId;
    return message.threadTs === root && message.ts !== root;
  }

  private async queryAll(
    method: "conversations.history" | "conversations.replies",
    baseParams: Readonly<Record<string, string>>,
  ): Promise<QueryResult> {
    const messages: SlackMessageRecord[] = [];
    const seenCursors = new Set<string>();
    let retentionLimited = false;
    let cursor = "";
    for (let page = 0; page < this.maxPages; page += 1) {
      const params = new URLSearchParams(baseParams);
      if (cursor !== "") params.set("cursor", cursor);

      const transport = await this.requestJson(
        `${SLACK_API_ROOT}${method}?${params.toString()}`,
        {
          method: "GET",
          headers: { authorization: `Bearer ${this.botToken}` },
        },
      );
      if (transport.outcome === "network_error") return { outcome: "uncertain" };
      const { response, parsed } = transport;
      if (response.status === 429) {
        return {
          outcome: "uncertain",
          reconcileAtMs: safeRetryAfterMs(response, this.now()),
        };
      }
      if (response.status >= 500) return { outcome: "uncertain" };
      if (parsed === undefined) return { outcome: "uncertain" };
      if (!response.ok || parsed.ok !== true) {
        const error = typeof parsed.error === "string" ? parsed.error : "";
        if (RATE_LIMIT_ERRORS.has(error)) {
          return {
            outcome: "uncertain",
            reconcileAtMs: safeRetryAfterMs(response, this.now()),
          };
        }
        // A deterministic auth/config/request rejection means this query cannot
        // prove effect absence. Escalate instead of looping or replaying.
        if (PERMANENT_SLACK_ERRORS.has(error)) return { outcome: "incomplete" };
        return { outcome: "uncertain" };
      }
      if (parsed.is_limited !== undefined && typeof parsed.is_limited !== "boolean") {
        return { outcome: "uncertain" };
      }
      retentionLimited ||= parsed.is_limited === true;
      if (parsed.has_more !== undefined && typeof parsed.has_more !== "boolean") {
        return { outcome: "uncertain" };
      }
      const pageMessages = parseMessages(parsed.messages);
      if (pageMessages === null) return { outcome: "uncertain" };
      messages.push(...pageMessages);

      const nextCursor = responseCursor(parsed);
      if (nextCursor === "") {
        return parsed.has_more === true
          ? { outcome: "incomplete" }
          : { outcome: "success", messages, retentionLimited };
      }
      if (seenCursors.has(nextCursor)) return { outcome: "incomplete" };
      seenCursors.add(nextCursor);
      cursor = nextCursor;
    }
    return { outcome: "incomplete" };
  }

  private async requestJson(url: string, init: RequestInit): Promise<HttpResult> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      // Workerd's global fetch rejects a class-instance receiver with
      // "Illegal invocation". Copy it to a local before calling so the
      // platform function is invoked without SlackSink as `this`.
      const fetchImpl = this.fetchImpl;
      const response = await fetchImpl(url, { ...init, signal: controller.signal });
      if (response.status === 429 || response.status >= 500) {
        return { outcome: "response", response };
      }
      try {
        const value = (await response.json()) as unknown;
        if (value === null || typeof value !== "object" || Array.isArray(value)) {
          return { outcome: "response", response };
        }
        const parsed = value as SlackApiResponse;
        return { outcome: "response", response, parsed };
      } catch {
        return { outcome: "response", response };
      }
    } catch {
      return { outcome: "network_error" };
    } finally {
      clearTimeout(timeout);
    }
  }
}
