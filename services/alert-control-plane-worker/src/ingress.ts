import type { CanonicalAlertEnvelope, TrustedAlertMetadata } from "./types";
import {
  canonicalEventDigest,
  createCanonicalEnvelope,
  ValidationError,
} from "./validation";

const DEFAULT_MAX_BODY_BYTES = 64 * 1024;

export interface IncidentAcknowledgement {
  readonly accepted: true;
  readonly incident_id: string | null;
  readonly generation: number | null;
  readonly lifecycle_state: string | null;
  readonly action: string;
  readonly occurrence_count: number;
  readonly state_version: number | null;
}

export interface IngressDependencies {
  readonly authenticate: (
    authorization: string | null,
  ) => Promise<TrustedAlertMetadata | null>;
  readonly incidentName: (envelope: CanonicalAlertEnvelope) => Promise<string>;
  readonly submit: (
    incidentName: string,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
  ) => Promise<IncidentAcknowledgement>;
  readonly now?: () => number;
  readonly maxBodyBytes?: number;
}

export class IngressConflictError extends Error {
  constructor(message = "event_id_conflict") {
    super(message);
    this.name = "IngressConflictError";
  }
}

export class IngressUnavailableError extends Error {
  constructor(message = "control_plane_unavailable") {
    super(message);
    this.name = "IngressUnavailableError";
  }
}

export function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "application/json; charset=utf-8",
    },
  });
}

async function readJsonBody(request: Request, maxBytes: number): Promise<unknown> {
  const contentType = request.headers.get("content-type")?.split(";", 1)[0]?.trim();
  if (contentType !== "application/json") {
    throw new ValidationError("content-type must be application/json");
  }

  const contentLength = request.headers.get("content-length");
  if (contentLength !== null) {
    if (!/^\d+$/.test(contentLength)) {
      throw new ValidationError("content-length is invalid");
    }
    if (Number(contentLength) > maxBytes) {
      throw new ValidationError("request body is too large");
    }
  }
  if (!request.body) throw new ValidationError("request body must be valid JSON");

  const chunks: Uint8Array[] = [];
  const reader = request.body.getReader();
  let length = 0;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      length += result.value.byteLength;
      if (length > maxBytes) {
        await reader.cancel("request body is too large");
        throw new ValidationError("request body is too large");
      }
      chunks.push(result.value);
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let raw: string;
  try {
    raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new ValidationError("request body must be UTF-8 JSON");
  }
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    throw new ValidationError("request body must be valid JSON");
  }
}

function errorResponse(error: unknown): Response {
  if (error instanceof ValidationError) {
    return jsonResponse({ error: "invalid_event", detail: error.message }, 400);
  }
  if (error instanceof IngressConflictError) {
    return jsonResponse({ error: "event_id_conflict" }, 409);
  }
  if (error instanceof IngressUnavailableError) {
    return jsonResponse({ error: error.message }, 503);
  }
  console.error("alert control plane ingress failed");
  return jsonResponse({ error: "internal_error" }, 500);
}

/** Handle the authenticated producer endpoint without owning incident state. */
export async function handleIngressRequest(
  request: Request,
  dependencies: IngressDependencies,
): Promise<Response> {
  const url = new URL(request.url);
  if (request.method !== "POST" || url.pathname !== "/v1/events") {
    return jsonResponse({ error: "not_found" }, 404);
  }

  try {
    const trusted = await dependencies.authenticate(request.headers.get("authorization"));
    if (!trusted) return jsonResponse({ error: "invalid_producer_identity" }, 401);

    const maxBodyBytes = dependencies.maxBodyBytes ?? DEFAULT_MAX_BODY_BYTES;
    if (!Number.isSafeInteger(maxBodyBytes) || maxBodyBytes <= 0) {
      throw new IngressUnavailableError("invalid_ingress_configuration");
    }
    const body = await readJsonBody(request, maxBodyBytes);
    const now = dependencies.now?.() ?? Date.now();
    const envelope = createCanonicalEnvelope(body, trusted, { now });
    const digest = await canonicalEventDigest(envelope.event);
    const incidentName = await dependencies.incidentName(envelope);
    const acknowledgement = await dependencies.submit(
      incidentName,
      envelope,
      digest,
    );
    return jsonResponse(acknowledgement, 202);
  } catch (error) {
    return errorResponse(error);
  }
}
