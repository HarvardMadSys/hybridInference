import { jwtVerify, SignJWT } from "jose";

import {
  DeploymentLookupError,
  type DeploymentKey,
  type TrustedDeploymentMetadata,
} from "./registry";
import type { StagingIngressConfig } from "./runtime-config";
import type {
  TrustedAlertMetadata,
  TrustedEnvironment,
  TrustedSource,
} from "./types";

const PRODUCER_TOKEN_ISSUER = "freeinference-alert-control-plane";
const PRODUCER_TOKEN_AUDIENCE = "alert-control-plane-producer";
const IDENTIFIER_RE = /^[A-Za-z0-9._:-]{1,256}$/;
const SERVICE_RE = /^[a-z][a-z0-9-]{0,127}$/;
const ARTIFACT_DIGEST_RE = /^sha256:[a-f0-9]{64}$/;

export interface ProducerCapabilityInput {
  readonly deployment: TrustedDeploymentMetadata;
  readonly source: TrustedSource;
  readonly principal: string;
}

export interface DeploymentRegistryLookup {
  lookup(key: DeploymentKey): Promise<TrustedDeploymentMetadata>;
}

function signingKey(material: string): Uint8Array {
  return new TextEncoder().encode(material);
}

function claim(
  payload: Record<string, unknown>,
  name: string,
  pattern: RegExp,
): string | null {
  const value = payload[name];
  return typeof value === "string" && pattern.test(value) ? value : null;
}

function environmentClaim(value: unknown): TrustedEnvironment | null {
  return value === "staging" || value === "production" ? value : null;
}

function sourceClaim(value: unknown): TrustedSource | null {
  return value === "gateway" || value === "status-monitor" ? value : null;
}

/** Mint a short-lived capability bound to one CI-attested deployment record. */
export async function mintProducerCapability(
  config: StagingIngressConfig,
  input: ProducerCapabilityInput,
  nowMs = Date.now(),
): Promise<string> {
  const nowSeconds = Math.floor(nowMs / 1_000);
  return new SignJWT({
    environment: input.deployment.environment,
    service: input.deployment.service,
    deployment_id: input.deployment.deploymentId,
    artifact_digest: input.deployment.artifactDigest,
    registry_version: input.deployment.registryVersion,
    source: input.source,
    principal: input.principal,
  })
    .setProtectedHeader({ alg: "HS256", typ: "JWT", kid: "v1" })
    .setIssuer(PRODUCER_TOKEN_ISSUER)
    .setAudience(PRODUCER_TOKEN_AUDIENCE)
    .setSubject(input.principal)
    .setJti(crypto.randomUUID())
    .setIssuedAt(nowSeconds)
    .setExpirationTime(nowSeconds + config.identity.producerTokenTtlSeconds)
    .sign(signingKey(config.identity.producerTokenSigningKey));
}

function bearerToken(authorization: string | null): string | null {
  if (authorization === null) return null;
  return /^Bearer ([A-Za-z0-9._~-]+)$/.exec(authorization)?.[1] ?? null;
}

/**
 * Authenticate an ingress capability, then prove its exact deployment is still
 * active. The producer never supplies trusted metadata in the event body.
 */
export async function authenticateProducer(
  authorization: string | null,
  config: StagingIngressConfig,
  registry: DeploymentRegistryLookup,
): Promise<TrustedAlertMetadata | null> {
  const token = bearerToken(authorization);
  if (token === null) return null;

  let payload: Record<string, unknown>;
  try {
    const verified = await jwtVerify(
      token,
      signingKey(config.identity.producerTokenSigningKey),
      {
        algorithms: ["HS256"],
        issuer: PRODUCER_TOKEN_ISSUER,
        audience: PRODUCER_TOKEN_AUDIENCE,
        clockTolerance: 5,
        requiredClaims: [
          "sub",
          "iat",
          "exp",
          "jti",
          "environment",
          "service",
          "deployment_id",
          "artifact_digest",
          "registry_version",
          "source",
          "principal",
        ],
      },
    );
    if (
      verified.protectedHeader.alg !== "HS256" ||
      verified.protectedHeader.typ !== "JWT" ||
      verified.protectedHeader.kid !== "v1"
    ) {
      return null;
    }
    payload = verified.payload;
  } catch {
    return null;
  }

  const environment = environmentClaim(payload.environment);
  const source = sourceClaim(payload.source);
  const service = claim(payload, "service", SERVICE_RE);
  const principal = claim(payload, "principal", IDENTIFIER_RE);
  const deploymentId = claim(payload, "deployment_id", IDENTIFIER_RE);
  const artifactDigest = claim(
    payload,
    "artifact_digest",
    ARTIFACT_DIGEST_RE,
  );
  const registryVersion = payload.registry_version;
  if (
    environment === null ||
    environment !== "staging" ||
    source === null ||
    service === null ||
    principal === null ||
    payload.sub !== principal ||
    deploymentId === null ||
    artifactDigest === null ||
    !Number.isSafeInteger(registryVersion) ||
    (registryVersion as number) <= 0
  ) {
    return null;
  }

  let deployment: TrustedDeploymentMetadata;
  try {
    deployment = await registry.lookup({
      environment,
      service,
      deploymentId,
      artifactDigest,
    });
  } catch (error) {
    if (error instanceof DeploymentLookupError) return null;
    throw error;
  }
  if (deployment.registryVersion !== registryVersion) return null;

  return {
    environment,
    source,
    principal,
    deployment_id: deployment.deploymentId,
    deployment_sha: deployment.deploymentSha,
    artifact_digest: deployment.artifactDigest,
    registry_version: deployment.registryVersion,
  };
}
