import {
  DeploymentLookupError,
  type DeploymentKey,
  DeploymentRegistry,
  DeploymentRegistryWriteError,
  type TrustedDeploymentMetadata,
  type VerifiedDeploymentCommand,
} from "./registry";
import {
  GitHubOidcDeploymentVerifier,
  type GitHubDeploymentAttestation,
} from "./github-oidc";
import {
  mintProducerCapability,
  type DeploymentRegistryLookup,
} from "./producer-identity";
import { jsonResponse } from "./ingress";
import { deriveDeploymentRegistryRouteName } from "./routing";
import {
  parseRuntimeConfig,
  type RuntimeEnvironment,
  type StagingIngressConfig,
} from "./runtime-config";
import type { TrustedSource } from "./types";

const MAX_ATTESTATION_BODY_BYTES = 32 * 1024;
const SERVICE_RE = /^[a-z][a-z0-9-]{0,127}$/;
const DEPLOYMENT_ID_RE = /^[^\u0000-\u001f\u007f]{1,256}$/u;
const ARTIFACT_DIGEST_RE = /^sha256:[a-f0-9]{64}$/;
const FULL_SHA_RE = /^[a-f0-9]{40}$/;
const STAGING_SYNTHETIC_SERVICE = "synthetic-alert-producer";
const STAGING_SYNTHETIC_SOURCE: TrustedSource = "gateway";
const STAGING_SYNTHETIC_PRINCIPAL = "staging-synthetic";

interface PublicActivateRequest {
  readonly action: "activate";
  readonly command: VerifiedDeploymentCommand & { readonly action: "activate" };
  readonly source: TrustedSource;
  readonly principal: string;
}

interface PublicRetireRequest {
  readonly action: "retire";
  readonly command: VerifiedDeploymentCommand & { readonly action: "retire" };
}

type PublicAttestationRequest = PublicActivateRequest | PublicRetireRequest;

function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): void {
  const keys = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  if (
    keys.length !== sortedExpected.length ||
    keys.some((key, index) => key !== sortedExpected[index])
  ) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
}

function stringField(
  value: unknown,
  pattern: RegExp,
): string {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  return value;
}

function timestamp(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  return value as number;
}

function deploymentKey(value: unknown): DeploymentKey {
  const input = record(value);
  exactKeys(input, [
    "environment",
    "service",
    "deployment_id",
    "artifact_digest",
  ]);
  if (input.environment !== "staging") {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  return {
    environment: "staging",
    service: stringField(input.service, SERVICE_RE),
    deploymentId: stringField(input.deployment_id, DEPLOYMENT_ID_RE),
    artifactDigest: stringField(input.artifact_digest, ARTIFACT_DIGEST_RE),
  };
}

function parsePublicAttestation(value: unknown): PublicAttestationRequest {
  const input = record(value);
  if (input.action === "activate") {
    exactKeys(input, [
      "action",
      "deployment",
      "deployment_sha",
      "activated_at",
      "source",
      "principal",
    ]);
    const key = deploymentKey(input.deployment);
    if (
      key.service !== STAGING_SYNTHETIC_SERVICE ||
      input.source !== STAGING_SYNTHETIC_SOURCE ||
      input.principal !== STAGING_SYNTHETIC_PRINCIPAL
    ) {
      throw new DeploymentRegistryWriteError("invalid_attestation");
    }
    return {
      action: "activate",
      command: {
        action: "activate",
        deployment: {
          ...key,
          deploymentSha: stringField(input.deployment_sha, FULL_SHA_RE),
          activatedAt: timestamp(input.activated_at),
        },
      },
      source: STAGING_SYNTHETIC_SOURCE,
      principal: STAGING_SYNTHETIC_PRINCIPAL,
    };
  }
  if (input.action === "retire") {
    exactKeys(input, ["action", "deployment", "retired_at"]);
    return {
      action: "retire",
      command: {
        action: "retire",
        deployment: deploymentKey(input.deployment),
        retiredAt: timestamp(input.retired_at),
      },
    };
  }
  throw new DeploymentRegistryWriteError("invalid_attestation");
}

async function jsonBody(request: Request): Promise<unknown> {
  if (
    request.headers.get("content-type")?.split(";", 1)[0]?.trim() !==
    "application/json"
  ) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  const contentLength = request.headers.get("content-length");
  if (
    contentLength !== null &&
    (!/^\d+$/.test(contentLength) ||
      Number(contentLength) > MAX_ATTESTATION_BODY_BYTES)
  ) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  if (request.body === null) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  const chunks: Uint8Array[] = [];
  const reader = request.body.getReader();
  let length = 0;
  try {
    for (;;) {
      const result = await reader.read();
      if (result.done) break;
      length += result.value.byteLength;
      if (length > MAX_ATTESTATION_BODY_BYTES) {
        await reader.cancel("attestation body is too large");
        throw new DeploymentRegistryWriteError("invalid_attestation");
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
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
}

function metadata(value: unknown): TrustedDeploymentMetadata {
  const input = record(value);
  const retiredAt = input.retiredAt;
  if (retiredAt !== null && !Number.isSafeInteger(retiredAt)) {
    throw new Error("invalid deployment registry response");
  }
  if (
    (input.environment !== "staging" && input.environment !== "production") ||
    typeof input.service !== "string" ||
    typeof input.deploymentId !== "string" ||
    typeof input.artifactDigest !== "string" ||
    typeof input.deploymentSha !== "string" ||
    !Number.isSafeInteger(input.activatedAt) ||
    !Number.isSafeInteger(input.registryVersion)
  ) {
    throw new Error("invalid deployment registry response");
  }
  return input as unknown as TrustedDeploymentMetadata;
}

function internalErrorResponse(error: unknown): Response {
  if (error instanceof DeploymentRegistryWriteError) {
    const status = error.code === "invalid_attestation" ? 401 : 409;
    return jsonResponse({ error: error.code }, status);
  }
  if (error instanceof DeploymentLookupError) {
    const status = error.code === "deployment_mismatch" ? 409 : 404;
    return jsonResponse({ error: error.code }, status);
  }
  console.error("deployment registry request failed");
  return jsonResponse({ error: "internal_error" }, 500);
}

function errorFromResponse(status: number, value: unknown): Error {
  const body =
    value !== null && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  const code = body.error;
  if (
    code === "unknown_deployment" ||
    code === "retired_deployment" ||
    code === "deployment_mismatch"
  ) {
    return new DeploymentLookupError(code);
  }
  if (
    code === "invalid_attestation" ||
    code === "deployment_conflict" ||
    code === "deployment_retired"
  ) {
    return new DeploymentRegistryWriteError(code);
  }
  return new Error(`deployment registry unavailable (${status})`);
}

export class BoundDeploymentRegistryClient
  implements DeploymentRegistryLookup
{
  constructor(private readonly config: StagingIngressConfig) {}

  async lookup(key: DeploymentKey): Promise<TrustedDeploymentMetadata> {
    return this.request(key, "/internal/lookup", { key });
  }

  async attest(
    authorization: string | null,
    command: VerifiedDeploymentCommand,
  ): Promise<TrustedDeploymentMetadata> {
    return this.request(
      command.deployment,
      "/internal/attest",
      { command },
      authorization,
    );
  }

  private async request(
    key: Pick<DeploymentKey, "environment" | "service">,
    path: string,
    body: unknown,
    authorization?: string | null,
  ): Promise<TrustedDeploymentMetadata> {
    const routeName = await deriveDeploymentRegistryRouteName(
      this.config.routeKey,
      key,
    );
    const objectId =
      this.config.identity.registryNamespace.idFromName(routeName);
    const headers = new Headers({ "content-type": "application/json" });
    if (authorization !== undefined && authorization !== null) {
      headers.set("authorization", authorization);
    }
    const response = await this.config.identity.registryNamespace
      .get(objectId)
      .fetch(
        new Request(`https://registry.internal${path}`, {
          method: "POST",
          headers,
          body: JSON.stringify(body),
        }),
      );
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      throw new Error("deployment registry returned invalid JSON");
    }
    if (!response.ok) throw errorFromResponse(response.status, value);
    return metadata(value);
  }
}

/** Public, OIDC-authenticated deployment activation/retirement endpoint. */
export async function handleDeploymentAttestationRequest(
  request: Request,
  config: StagingIngressConfig,
): Promise<Response> {
  const url = new URL(request.url);
  if (
    request.method !== "POST" ||
    url.pathname !== "/v1/deployments/attest"
  ) {
    return jsonResponse({ error: "not_found" }, 404);
  }

  try {
    const input = parsePublicAttestation(await jsonBody(request));
    const client = new BoundDeploymentRegistryClient(config);
    const deployment = await client.attest(
      request.headers.get("authorization"),
      input.command,
    );
    if (input.action === "retire") {
      return jsonResponse({ deployment });
    }
    const producerToken = await mintProducerCapability(config, {
      deployment,
      source: input.source,
      principal: input.principal,
    });
    return jsonResponse({
      deployment,
      producer_token: producerToken,
      expires_in: config.identity.producerTokenTtlSeconds,
    });
  } catch (error) {
    return internalErrorResponse(error);
  }
}

/** SQLite registry object; raw OIDC is verified here before any write. */
export class DeploymentRegistryDurableObject {
  private readonly registry: DeploymentRegistry<GitHubDeploymentAttestation> | null;

  constructor(
    state: DurableObjectState,
    env: RuntimeEnvironment,
  ) {
    const config = parseRuntimeConfig(env);
    this.registry =
      config.mode === "staging-ingress"
        ? new DeploymentRegistry(
            state.storage,
            new GitHubOidcDeploymentVerifier(config.identity.githubOidc),
          )
        : null;
  }

  async fetch(request: Request): Promise<Response> {
    if (this.registry === null) {
      return jsonResponse({ error: "control_plane_dormant" }, 503);
    }
    const url = new URL(request.url);
    try {
      if (
        request.method === "POST" &&
        url.pathname === "/internal/attest"
      ) {
        const input = record(await jsonBody(request));
        exactKeys(input, ["command"]);
        const deployment = await this.registry.apply({
          authorization: request.headers.get("authorization"),
          command: input.command as VerifiedDeploymentCommand,
        });
        return jsonResponse(deployment);
      }
      if (
        request.method === "POST" &&
        url.pathname === "/internal/lookup"
      ) {
        const input = record(await jsonBody(request));
        exactKeys(input, ["key"]);
        return jsonResponse(
          this.registry.lookup(input.key as DeploymentKey),
        );
      }
      return jsonResponse({ error: "not_found" }, 404);
    } catch (error) {
      return internalErrorResponse(error);
    }
  }
}
