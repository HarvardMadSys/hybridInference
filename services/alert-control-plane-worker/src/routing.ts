import type { AlertEvent, TrustedAlertMetadata } from "./types";

const ROUTE_DOMAIN = "alert-control-plane:route-key:v1";
const QUOTA_ROUTE_DOMAIN = "alert-control-plane:principal-quota:v1";
const REGISTRY_ROUTE_DOMAIN = "alert-control-plane:deployment-registry:v1";
const MINIMUM_ROUTE_KEY_BYTES = 32;
const encoder = new TextEncoder();

export interface IncidentRouteIdentity {
  readonly environment: TrustedAlertMetadata["environment"];
  readonly principal: string;
  readonly fingerprint: string;
}

function lengthPrefixed(value: string): Uint8Array {
  const encoded = encoder.encode(value.normalize("NFC"));
  const result = new Uint8Array(4 + encoded.length);
  new DataView(result.buffer).setUint32(0, encoded.length, false);
  result.set(encoded, 4);
  return result;
}

function concatenate(parts: readonly Uint8Array[]): Uint8Array {
  const length = parts.reduce((total, part) => total + part.length, 0);
  const result = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

function ownedBuffer(bytes: Uint8Array): ArrayBuffer {
  const buffer = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(buffer).set(bytes);
  return buffer;
}

/**
 * Encode route material as uint32-big-endian UTF-8 byte lengths followed by bytes.
 * This remains unexported from logs and URLs; only its HMAC is used as a DO name.
 */
export function encodeIncidentRouteMaterial(identity: IncidentRouteIdentity): Uint8Array {
  return concatenate([
    lengthPrefixed(identity.environment),
    lengthPrefixed(identity.principal),
    lengthPrefixed(identity.fingerprint),
  ]);
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

async function deriveRouteName(
  routeKey: string | Uint8Array,
  domain: string,
  material: Uint8Array,
): Promise<string> {
  const keyBytes = typeof routeKey === "string" ? encoder.encode(routeKey) : routeKey;
  if (keyBytes.length < MINIMUM_ROUTE_KEY_BYTES) {
    throw new Error(`route key must contain at least ${MINIMUM_ROUTE_KEY_BYTES} bytes`);
  }
  const key = await crypto.subtle.importKey(
    "raw",
    ownedBuffer(keyBytes),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signedMaterial = concatenate([lengthPrefixed(domain), material]);
  const signature = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, ownedBuffer(signedMaterial)),
  );
  return base64Url(signature);
}

/** Produce the opaque, stable name passed to DurableObjectNamespace.idFromName. */
export async function deriveIncidentRouteName(
  routeKey: string | Uint8Array,
  identity: IncidentRouteIdentity,
): Promise<string> {
  return deriveRouteName(
    routeKey,
    ROUTE_DOMAIN,
    encodeIncidentRouteMaterial(identity),
  );
}

/** Derive one opaque quota-object name for a trusted environment + principal. */
export async function derivePrincipalQuotaRouteName(
  routeKey: string | Uint8Array,
  identity: { readonly environment: string; readonly principal: string },
): Promise<string> {
  return deriveRouteName(
    routeKey,
    QUOTA_ROUTE_DOMAIN,
    concatenate([
      lengthPrefixed(identity.environment),
      lengthPrefixed(identity.principal),
    ]),
  );
}

/** Derive one opaque registry-object name for a trusted environment + service. */
export async function deriveDeploymentRegistryRouteName(
  routeKey: string | Uint8Array,
  identity: { readonly environment: string; readonly service: string },
): Promise<string> {
  return deriveRouteName(
    routeKey,
    REGISTRY_ROUTE_DOMAIN,
    concatenate([
      lengthPrefixed(identity.environment),
      lengthPrefixed(identity.service),
    ]),
  );
}

export async function routeNameForEnvelope(
  routeKey: string | Uint8Array,
  trusted: TrustedAlertMetadata,
  event: AlertEvent,
): Promise<string> {
  return deriveIncidentRouteName(routeKey, {
    environment: trusted.environment,
    principal: trusted.principal,
    fingerprint: event.fingerprint,
  });
}
