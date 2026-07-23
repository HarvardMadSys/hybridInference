import { jsonResponse } from "./ingress";
import {
  PrincipalQuota,
  QuotaLeaseError,
  type QuotaReservationIdentity,
  type QuotaReservationResult,
} from "./quota";
import type {
  ActionClaim,
  ActionExecutionResult,
  ActionExecutor,
} from "./outbox";
import { derivePrincipalQuotaRouteName } from "./routing";
import {
  parseRuntimeConfig,
  type RuntimeEnvironment,
} from "./runtime-config";

const MAX_INTERNAL_BODY_BYTES = 4 * 1024;

interface QuotaService {
  reserve(identity: QuotaReservationIdentity): QuotaReservationResult;
  confirm(identity: QuotaReservationIdentity, leaseEpoch: number): unknown;
  release(identity: QuotaReservationIdentity, leaseEpoch: number): unknown;
}

interface QuotaLeaseRequest extends QuotaReservationIdentity {
  readonly leaseEpoch: number;
}

type QuotaCallResult =
  | { readonly outcome: "response"; readonly response: Response; readonly body?: unknown }
  | { readonly outcome: "uncertain" };

function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("invalid quota request");
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  input: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  const actual = Object.keys(input).sort();
  const wanted = [...expected].sort();
  return (
    actual.length === wanted.length &&
    actual.every((key, index) => key === wanted[index])
  );
}

function identityFromRecord(
  input: Record<string, unknown>,
): QuotaReservationIdentity {
  if (
    typeof input.environment !== "string" ||
    typeof input.principal !== "string" ||
    typeof input.incident_id !== "string" ||
    typeof input.generation !== "number"
  ) {
    throw new TypeError("invalid quota request");
  }
  return {
    environment: input.environment,
    principal: input.principal,
    incidentId: input.incident_id,
    generation: input.generation,
  };
}

function leaseRequestFromRecord(
  input: Record<string, unknown>,
): QuotaLeaseRequest {
  if (typeof input.lease_epoch !== "number") {
    throw new TypeError("invalid quota request");
  }
  return {
    ...identityFromRecord(input),
    leaseEpoch: input.lease_epoch,
  };
}

async function requestRecord(request: Request): Promise<Record<string, unknown>> {
  const contentType = request.headers.get("content-type")?.split(";", 1)[0]?.trim();
  if (contentType !== "application/json") throw new TypeError("invalid quota request");
  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > MAX_INTERNAL_BODY_BYTES) {
    throw new TypeError("invalid quota request");
  }
  try {
    return record(JSON.parse(raw) as unknown);
  } catch {
    throw new TypeError("invalid quota request");
  }
}

/**
 * Handle binding-only quota requests. This function is exported so the exact
 * network contract can be tested with the in-memory quota core.
 */
export async function handlePrincipalQuotaRequest(
  quota: QuotaService,
  request: Request,
): Promise<Response> {
  const url = new URL(request.url);
  if (request.method !== "POST") {
    return jsonResponse({ error: "not_found" }, 404);
  }

  try {
    const input = await requestRecord(request);
    if (url.pathname === "/internal/reserve") {
      if (
        !exactKeys(input, [
          "environment",
          "principal",
          "incident_id",
          "generation",
        ])
      ) {
        throw new TypeError("invalid quota request");
      }
      const result = quota.reserve(identityFromRecord(input));
      if (!result.admitted) {
        return jsonResponse({ admitted: false }, 200);
      }
      return jsonResponse(
        {
          admitted: true,
          lease_epoch: result.reservation.leaseEpoch,
        },
        200,
      );
    }

    if (
      url.pathname === "/internal/confirm" ||
      url.pathname === "/internal/release"
    ) {
      if (
        !exactKeys(input, [
          "environment",
          "principal",
          "incident_id",
          "generation",
          "lease_epoch",
        ])
      ) {
        throw new TypeError("invalid quota request");
      }
      const parsed = leaseRequestFromRecord(input);
      if (url.pathname === "/internal/confirm") {
        quota.confirm(parsed, parsed.leaseEpoch);
        return jsonResponse(
          { confirmed: true, lease_epoch: parsed.leaseEpoch },
          200,
        );
      }
      quota.release(parsed, parsed.leaseEpoch);
      return jsonResponse({ released: true }, 200);
    }

    return jsonResponse({ error: "not_found" }, 404);
  } catch (error) {
    if (error instanceof QuotaLeaseError) {
      const status = error.code === "invalid_reservation" ? 400 : 409;
      return jsonResponse({ error: error.code }, status);
    }
    if (error instanceof TypeError) {
      return jsonResponse({ error: "invalid_reservation" }, 400);
    }
    return jsonResponse({ error: "quota_internal_error" }, 500);
  }
}

/** SQLite-backed principal quota authority, addressed only through a DO binding. */
export class PrincipalQuotaDurableObject {
  private readonly quota: PrincipalQuota | null;

  constructor(state: DurableObjectState, env: RuntimeEnvironment) {
    const config = parseRuntimeConfig(env);
    this.quota =
      config.mode === "staging-runtime" || config.mode === "staging-ingress"
        ? new PrincipalQuota(state.storage, {
            activeLimit: config.quota.activeLimit,
            pendingLeaseMs: config.quota.pendingLeaseMs,
          })
        : null;
  }

  async fetch(request: Request): Promise<Response> {
    if (this.quota === null) {
      return jsonResponse({ error: "quota_configuration_invalid" }, 503);
    }
    return handlePrincipalQuotaRequest(this.quota, request);
  }
}

function actionIdentity(claim: ActionClaim): QuotaReservationIdentity {
  const payload = claim.action.payload;
  if (
    typeof payload.environment !== "string" ||
    typeof payload.principal !== "string" ||
    typeof payload.incident_id !== "string" ||
    typeof payload.generation !== "number" ||
    payload.generation !== claim.action.generation
  ) {
    throw new TypeError("invalid quota action");
  }
  return {
    environment: payload.environment,
    principal: payload.principal,
    incidentId: payload.incident_id,
    generation: payload.generation,
  };
}

function leaseEpoch(claim: ActionClaim): number {
  const value = claim.action.payload.lease_epoch;
  if (!Number.isSafeInteger(value) || Number(value) < 1) {
    throw new TypeError("invalid quota action");
  }
  return Number(value);
}

function requestPayload(
  identity: QuotaReservationIdentity,
  epoch?: number,
): Record<string, unknown> {
  return {
    environment: identity.environment,
    principal: identity.principal,
    incident_id: identity.incidentId,
    generation: identity.generation,
    ...(epoch === undefined ? {} : { lease_epoch: epoch }),
  };
}

function responseRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function quotaFailure(result: QuotaCallResult): ActionExecutionResult | null {
  if (result.outcome === "uncertain") {
    return { outcome: "uncertain", error: "quota_request_uncertain" };
  }
  if (result.response.ok) return null;
  const body = responseRecord(result.body);
  const code = typeof body?.error === "string" ? body.error : "";
  if (
    result.response.status === 400 ||
    code === "invalid_reservation"
  ) {
    return { outcome: "failed", error: "quota_action_invalid" };
  }
  if (
    code === "stale_lease" ||
    code === "expired_lease" ||
    code === "released_lease"
  ) {
    return {
      outcome: "manual_reconciliation_required",
      error: "quota_lease_mismatch",
    };
  }
  if (code === "unknown_reservation") {
    return {
      outcome: "manual_reconciliation_required",
      error: "quota_reservation_missing",
    };
  }
  if (
    result.response.status === 503 ||
    code === "quota_configuration_invalid"
  ) {
    return { outcome: "failed", error: "quota_configuration_invalid" };
  }
  return { outcome: "uncertain", error: "quota_request_uncertain" };
}

/**
 * Bridge incident outbox quota actions to the principal-scoped quota object.
 * A successful reserve intentionally enters reconciliation before confirmation,
 * keeping the parent blocked until the cross-object lease is durable on both
 * sides.
 */
export class QuotaActionExecutor implements ActionExecutor {
  private readonly now: () => number;

  constructor(
    private readonly namespace: DurableObjectNamespace,
    private readonly routeKey: string,
    now: (() => number) | undefined = undefined,
  ) {
    this.now = now ?? Date.now;
  }

  async execute(claim: ActionClaim): Promise<ActionExecutionResult> {
    if (
      claim.action.type !== "reserve_quota" &&
      claim.action.type !== "release_quota"
    ) {
      return { outcome: "failed", error: "quota_action_invalid" };
    }

    let identity: QuotaReservationIdentity;
    try {
      identity = actionIdentity(claim);
    } catch {
      return { outcome: "failed", error: "quota_action_invalid" };
    }

    if (claim.action.type === "release_quota") {
      let epoch: number;
      try {
        epoch = leaseEpoch(claim);
      } catch {
        return { outcome: "failed", error: "quota_action_invalid" };
      }
      const released = await this.call("release", identity, epoch);
      const failure = quotaFailure(released);
      if (failure !== null) return failure;
      if (
        released.outcome !== "response" ||
        responseRecord(released.body)?.released !== true
      ) {
        return { outcome: "uncertain", error: "quota_response_uncertain" };
      }
      return { outcome: "success", result: {} };
    }

    const reserved = await this.call("reserve", identity);
    const reservationFailure = quotaFailure(reserved);
    if (reservationFailure !== null) return reservationFailure;
    if (reserved.outcome !== "response") {
      return { outcome: "uncertain", error: "quota_request_uncertain" };
    }
    const reservation = responseRecord(reserved.body);
    if (reservation?.admitted === false) {
      return { outcome: "success", result: { admitted: false } };
    }
    if (
      reservation?.admitted !== true ||
      !Number.isSafeInteger(reservation.lease_epoch) ||
      Number(reservation.lease_epoch) < 1
    ) {
      return { outcome: "uncertain", error: "quota_response_uncertain" };
    }
    const epoch = Number(reservation.lease_epoch);

    if (claim.mode === "execute") {
      return {
        outcome: "uncertain",
        error: "quota_confirmation_pending",
        reconcileAtMs: this.now(),
      };
    }

    const confirmed = await this.call("confirm", identity, epoch);
    const confirmationFailure = quotaFailure(confirmed);
    if (confirmationFailure !== null) return confirmationFailure;
    if (confirmed.outcome !== "response") {
      return { outcome: "uncertain", error: "quota_request_uncertain" };
    }
    const confirmation = responseRecord(confirmed.body);
    if (
      confirmation?.confirmed !== true ||
      confirmation.lease_epoch !== epoch
    ) {
      return { outcome: "uncertain", error: "quota_response_uncertain" };
    }
    return {
      outcome: "success",
      result: { admitted: true, lease_epoch: epoch },
    };
  }

  private async call(
    operation: "reserve" | "confirm" | "release",
    identity: QuotaReservationIdentity,
    epoch?: number,
  ): Promise<QuotaCallResult> {
    let routeName: string;
    try {
      routeName = await derivePrincipalQuotaRouteName(
        this.routeKey,
        identity,
      );
    } catch {
      return {
        outcome: "response",
        response: new Response(null, { status: 400 }),
        body: { error: "invalid_reservation" },
      };
    }

    try {
      const objectId = this.namespace.idFromName(routeName);
      const response = await this.namespace.get(objectId).fetch(
        new Request(`https://quota.internal/internal/${operation}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(requestPayload(identity, epoch)),
        }),
      );
      let body: unknown;
      try {
        body = await response.json();
      } catch {
        body = undefined;
      }
      return { outcome: "response", response, body };
    } catch {
      return { outcome: "uncertain" };
    }
  }
}
