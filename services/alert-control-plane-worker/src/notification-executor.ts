/**
 * Wiring layer between the generic outbox and a single {@link NotificationSink}.
 *
 * Dependency direction is `outbox/store -> executor -> notification contract`.
 * This module may import the store and outbox; the notification contract and any
 * concrete sink must not. Fencing stays in the generic outbox: the executor only
 * projects an allowlisted, platform-neutral action and serializes the result.
 */
import {
  type DeliveryRef,
  deliveryRefEquals,
  isNotificationActionType,
  type NotificationAction,
  type NotificationActionResult,
  type NotificationReceipt,
  type NotificationSink,
  parseDeliveryRef,
} from "./notification";
import type { ActionClaim, ActionExecutionResult, ActionExecutor } from "./outbox";
import type { IncidentStore, PendingAction } from "./store";
import { canonicalDigest } from "./validation";

/**
 * Only platform-agnostic, already-validated semantic fields are forwarded to a
 * sink. Fence/platform fields such as `state_version`, `resolution_epoch`,
 * `delivery_ref`, and any Slack-specific keys are intentionally excluded by
 * being absent from this allowlist.
 */
const NOTIFICATION_PAYLOAD_ALLOWLIST: ReadonlySet<string> = new Set([
  "incident_id",
  "generation",
  "occurrence_count",
  "envelope",
  "first_seen",
  "last_seen",
  "analysis",
]);

/**
 * Project a stored {@link PendingAction} into a platform-neutral
 * {@link NotificationAction}. Only notification action types are valid. The
 * sink-facing payload is built by allowlist, and the delivery reference
 * precondition matches the doc: `post_parent` must carry a `null` reference,
 * while the other three require the existing non-null reference.
 */
export async function projectNotificationAction(
  action: PendingAction,
  deliveryRef: DeliveryRef | null,
  sinkId: string,
): Promise<NotificationAction> {
  if (!isNotificationActionType(action.type)) {
    throw new Error(
      `projectNotificationAction received non-notification action: ${action.type}`,
    );
  }
  const type = action.type;

  if (type === "post_parent") {
    if (deliveryRef !== null) {
      throw new Error("post_parent must be projected with a null delivery reference");
    }
  } else if (deliveryRef === null) {
    throw new Error(`${type} requires an existing delivery reference`);
  }
  if (deliveryRef !== null && deliveryRef.sinkId !== sinkId) {
    throw new Error("delivery reference sinkId does not match the target sink");
  }

  const payload: Record<string, unknown> = {};
  for (const key of Object.keys(action.payload)) {
    if (NOTIFICATION_PAYLOAD_ALLOWLIST.has(key)) {
      payload[key] = action.payload[key];
    }
  }

  const payloadDigest = await canonicalDigest({
    type,
    incidentId: action.incidentId,
    generation: action.generation,
    payload,
  });

  return {
    type,
    actionId: action.actionId,
    sinkId,
    incidentId: action.incidentId,
    generation: action.generation,
    payloadDigest,
    deliveryRef: type === "post_parent" ? null : deliveryRef,
    payload,
  };
}

const STABLE_ERROR_CODE_RE = /^[a-z][a-z0-9_]{0,63}$/;

/**
 * A sink must return a stable, redacted error code. Any value that is not a
 * narrow snake_case token is replaced so a response body, URL, or token can
 * never reach the outbox's persisted `last_error`.
 */
export function stableErrorCode(code: string): string {
  return STABLE_ERROR_CODE_RE.test(code) ? code : "sink_error_unclassified";
}

/** Map a sink result onto the generic outbox result union. */
export function serializeNotificationResult(
  result: NotificationActionResult,
): ActionExecutionResult {
  switch (result.outcome) {
    case "success":
      return { outcome: "success", result: { receipt: result.receipt } };
    case "retry":
      return {
        outcome: "retry",
        error: stableErrorCode(result.errorCode),
        retryAtMs: result.retryAtMs,
      };
    case "uncertain":
      return {
        outcome: "uncertain",
        error: stableErrorCode(result.errorCode),
        reconcileAtMs: result.reconcileAtMs,
      };
    case "manual_reconciliation_required":
      return {
        outcome: "manual_reconciliation_required",
        error: stableErrorCode(result.errorCode),
      };
    case "failed":
      return { outcome: "failed", error: stableErrorCode(result.errorCode) };
  }
}

/**
 * Validate a success receipt before it can be committed. Returns a stable error
 * code when the receipt is unusable, or `null` when it is safe to commit.
 *
 * A structurally valid but mismatched reference must never be accepted: a wrong
 * `post_recovery` reference would otherwise advance the incident to resolved,
 * and a wrong `post_parent` reference would bind the incident to a foreign sink.
 */
export function receiptViolation(
  receipt: NotificationReceipt,
  expectedRef: DeliveryRef | null,
  sink: Pick<NotificationSink, "sinkId" | "platform">,
): string | null {
  let ref: DeliveryRef;
  try {
    ref = parseDeliveryRef(receipt.deliveryRef);
  } catch {
    return "receipt_reference_invalid";
  }
  if (ref.sinkId !== sink.sinkId || ref.platform !== sink.platform) {
    return "receipt_sink_mismatch";
  }
  if (expectedRef !== null && !deliveryRefEquals(expectedRef, ref)) {
    return "receipt_reference_mismatch";
  }
  return null;
}

/**
 * Executes notification actions through a single sink. It reads the authoritative
 * delivery reference from the store but never mutates incident state; the outbox
 * commits the result and the incident lifecycle hook applies it. Sinks must never
 * throw, so this executor only surfaces what the sink returns (plus projection
 * invariant violations).
 */
export class NotificationActionExecutor implements ActionExecutor {
  constructor(
    private readonly store: IncidentStore,
    private readonly sink: NotificationSink,
  ) {}

  async execute(claim: ActionClaim): Promise<ActionExecutionResult> {
    const action = claim.action;
    if (!isNotificationActionType(action.type)) {
      throw new Error("NotificationActionExecutor received non-notification action");
    }
    const generation = this.store.getGeneration(action.generation);
    const existingRef = generation?.deliveryRef ?? null;
    const projected = await projectNotificationAction(
      action,
      action.type === "post_parent" ? null : existingRef,
      this.sink.sinkId,
    );
    const result = await this.sink.execute(projected, claim.mode);
    if (result.outcome === "success") {
      // post_parent may mint a new reference (existingRef null on first open),
      // but a retry must match it; every other action must echo the input ref.
      const expectedRef = action.type === "post_parent" ? existingRef : projected.deliveryRef;
      const violation = receiptViolation(result.receipt, expectedRef, this.sink);
      if (violation !== null) {
        return { outcome: "manual_reconciliation_required", error: violation };
      }
    }
    return serializeNotificationResult(result);
  }
}
