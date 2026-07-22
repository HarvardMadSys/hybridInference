/**
 * Platform-agnostic notification contract for the incident control plane.
 *
 * This module is a pure contract: it MUST NOT import from the store, incident
 * state machine, outbox, renderer, or worker entrypoint. The incident authority
 * produces platform-neutral {@link NotificationAction}s and a single configured
 * {@link NotificationSink} renders and delivers them. A static import-boundary
 * test asserts this file stays dependency-free.
 */

/** The only notification platform currently implemented. */
export type NotificationPlatform = "slack";

/** Runtime allowlist mirroring {@link NotificationPlatform}. */
export const SUPPORTED_NOTIFICATION_PLATFORMS = ["slack"] as const;

/**
 * A persisted, verifiable reference to an external delivery location. It only
 * describes *where* a notification lives; message/conversation identifiers are
 * opaque strings to the state machine and never carry tokens or user text.
 */
export interface DeliveryRef {
  readonly schemaVersion: 1;
  readonly sinkId: string;
  readonly platform: NotificationPlatform;
  readonly destinationId: string;
  readonly messageId: string;
  readonly conversationId?: string;
}

/** The four platform-agnostic notification intents. */
export type NotificationActionType =
  | "post_parent"
  | "update_parent"
  | "post_recovery"
  | "post_analysis";

/** Runtime allowlist mirroring {@link NotificationActionType}. */
export const NOTIFICATION_ACTION_TYPES = [
  "post_parent",
  "update_parent",
  "post_recovery",
  "post_analysis",
] as const satisfies readonly NotificationActionType[];

/** Narrow an arbitrary action type string to a notification action type. */
export function isNotificationActionType(value: string): value is NotificationActionType {
  return (NOTIFICATION_ACTION_TYPES as readonly string[]).includes(value);
}

/** A single, platform-neutral notification intent handed to a sink. */
export interface NotificationAction {
  readonly type: NotificationActionType;
  readonly actionId: string;
  readonly sinkId: string;
  readonly incidentId: string;
  readonly generation: number;
  readonly payloadDigest: string;
  readonly deliveryRef: DeliveryRef | null;
  readonly payload: Readonly<Record<string, unknown>>;
}

/** Whether the sink should attempt the effect or reconcile a prior attempt. */
export type NotificationAttemptMode = "execute" | "reconcile";

/** Successful delivery receipt returned by a sink. */
export interface NotificationReceipt {
  readonly deliveryRef: DeliveryRef;
  readonly externalEffectId?: string;
}

/**
 * The outcome of a single sink attempt. `retry` means the platform provably did
 * not accept the request; `uncertain` means the request may have taken effect
 * and must be reconciled before any replay.
 */
export type NotificationActionResult =
  | { readonly outcome: "success"; readonly receipt: NotificationReceipt }
  | { readonly outcome: "retry"; readonly errorCode: string; readonly retryAtMs?: number }
  | {
      readonly outcome: "uncertain";
      readonly errorCode: string;
      readonly reconcileAtMs?: number;
    }
  | {
      readonly outcome: "manual_reconciliation_required";
      readonly errorCode: string;
    }
  | { readonly outcome: "failed"; readonly errorCode: string };

/**
 * A notification sink renders and delivers a single {@link NotificationAction}.
 * Sinks own tokens, channels, threads, and scoped reconciliation, but never own
 * incident state. Implementations MUST catch and classify every error and MUST
 * NOT throw; the generic outbox owns claim, lease, backoff, and fencing.
 */
export interface NotificationSink {
  readonly sinkId: string;
  readonly platform: NotificationPlatform;

  execute(
    action: NotificationAction,
    mode: NotificationAttemptMode,
  ): Promise<NotificationActionResult>;
}

/** Raised when an untrusted value cannot be parsed into a {@link DeliveryRef}. */
export class DeliveryRefValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DeliveryRefValidationError";
  }
}

const DELIVERY_REF_KEYS: ReadonlySet<string> = new Set([
  "schemaVersion",
  "sinkId",
  "platform",
  "destinationId",
  "messageId",
  "conversationId",
]);

const MAX_DELIVERY_REF_FIELD_LENGTH = 256;
const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f-\u009f]/;

function refString(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new DeliveryRefValidationError(`${field} must be a string`);
  }
  if (value.length === 0) {
    throw new DeliveryRefValidationError(`${field} must not be empty`);
  }
  if (value.length > MAX_DELIVERY_REF_FIELD_LENGTH) {
    throw new DeliveryRefValidationError(
      `${field} must be at most ${MAX_DELIVERY_REF_FIELD_LENGTH} characters`,
    );
  }
  if (CONTROL_CHAR_RE.test(value)) {
    throw new DeliveryRefValidationError(`${field} must not contain control characters`);
  }
  return value;
}

/**
 * Strictly parse an untrusted value into a {@link DeliveryRef}. Rejects
 * non-objects, unknown keys, a non-1 `schemaVersion`, unsupported platforms, and
 * missing/empty/oversized/control-bearing string fields. Never calls an external
 * API.
 */
export function parseDeliveryRef(value: unknown): DeliveryRef {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new DeliveryRefValidationError("delivery reference must be an object");
  }
  const input = value as Record<string, unknown>;
  for (const key of Object.keys(input)) {
    if (!DELIVERY_REF_KEYS.has(key)) {
      throw new DeliveryRefValidationError(
        `delivery reference contains unsupported field: ${key}`,
      );
    }
  }
  if (input.schemaVersion !== 1) {
    throw new DeliveryRefValidationError("delivery reference schemaVersion must be 1");
  }
  const platform = input.platform;
  if (
    typeof platform !== "string" ||
    !(SUPPORTED_NOTIFICATION_PLATFORMS as readonly string[]).includes(platform)
  ) {
    throw new DeliveryRefValidationError("delivery reference platform is unsupported");
  }
  const sinkId = refString(input.sinkId, "delivery reference sinkId");
  const destinationId = refString(input.destinationId, "delivery reference destinationId");
  const messageId = refString(input.messageId, "delivery reference messageId");
  let conversationId: string | undefined;
  if (Object.hasOwn(input, "conversationId") && input.conversationId !== undefined) {
    conversationId = refString(input.conversationId, "delivery reference conversationId");
  }
  return {
    schemaVersion: 1,
    sinkId,
    platform: platform as NotificationPlatform,
    destinationId,
    messageId,
    ...(conversationId !== undefined ? { conversationId } : {}),
  };
}

/** Full field-by-field equality, including optional `conversationId` presence. */
export function deliveryRefEquals(a: DeliveryRef, b: DeliveryRef): boolean {
  return (
    a.schemaVersion === b.schemaVersion &&
    a.sinkId === b.sinkId &&
    a.platform === b.platform &&
    a.destinationId === b.destinationId &&
    a.messageId === b.messageId &&
    a.conversationId === b.conversationId
  );
}
