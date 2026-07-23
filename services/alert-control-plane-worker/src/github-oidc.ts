import {
  createRemoteJWKSet,
  jwtVerify,
  type JWTPayload,
} from "jose";

import {
  type DeploymentAttestationVerifier,
  DeploymentRegistryWriteError,
  type VerifiedDeploymentCommand,
} from "./registry";
import type { StagingIngressConfig } from "./runtime-config";

const GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com";
const GITHUB_OIDC_JWKS =
  "https://token.actions.githubusercontent.com/.well-known/jwks";
const MAX_ATTESTATION_CLOCK_SKEW_MS = 5 * 60 * 1_000;
const FULL_SHA_RE = /^[a-f0-9]{40}$/;
const NUMERIC_CLAIM_RE = /^[1-9][0-9]*$/;
const STAGING_SYNTHETIC_SERVICE = "synthetic-alert-producer";

const githubJwks = createRemoteJWKSet(new URL(GITHUB_OIDC_JWKS), {
  cooldownDuration: 30_000,
  cacheMaxAge: 10 * 60 * 1_000,
  timeoutDuration: 5_000,
});

export interface GitHubDeploymentAttestation {
  readonly authorization: string | null;
  readonly command: VerifiedDeploymentCommand;
}

export type VerifyGitHubToken = (
  token: string,
  audience: string,
) => Promise<JWTPayload>;

async function verifyGitHubToken(
  token: string,
  audience: string,
): Promise<JWTPayload> {
  const result = await jwtVerify(token, githubJwks, {
    algorithms: ["RS256"],
    issuer: GITHUB_OIDC_ISSUER,
    audience,
    clockTolerance: 10,
    maxTokenAge: "5m",
    requiredClaims: [
      "sub",
      "iat",
      "exp",
      "jti",
      "repository",
      "repository_id",
      "repository_owner_id",
      "workflow_ref",
      "sha",
      "ref",
      "environment",
      "event_name",
      "actor_id",
      "run_id",
      "run_attempt",
      "runner_environment",
    ],
  });
  return result.payload;
}

function bearerToken(authorization: string | null): string {
  if (authorization === null) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  const match = /^Bearer ([A-Za-z0-9._~-]+)$/.exec(authorization);
  if (match?.[1] === undefined) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  return match[1];
}

function stringClaim(payload: JWTPayload, name: string): string {
  const value = payload[name];
  if (typeof value !== "string" || value.length === 0) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  return value;
}

function audienceMatches(value: JWTPayload["aud"], expected: string): boolean {
  return (
    value === expected ||
    (Array.isArray(value) && value.includes(expected))
  );
}

function attestationTimestamp(command: VerifiedDeploymentCommand): number {
  return command.action === "activate"
    ? command.deployment.activatedAt
    : command.retiredAt;
}

/**
 * Verify a deployment command inside the registry object. Signature/expiry are
 * checked against GitHub's rotating JWKS before any repository state is read or
 * written, and every accepted claim is pinned to the reviewed staging workflow.
 */
export class GitHubOidcDeploymentVerifier
  implements DeploymentAttestationVerifier<GitHubDeploymentAttestation>
{
  constructor(
    private readonly config: StagingIngressConfig["identity"]["githubOidc"],
    private readonly verifyToken: VerifyGitHubToken = verifyGitHubToken,
    private readonly now: () => number = Date.now,
  ) {}

  async verify(
    attestation: GitHubDeploymentAttestation,
  ): Promise<VerifiedDeploymentCommand> {
    try {
      const token = bearerToken(attestation.authorization);
      const claims = await this.verifyToken(token, this.config.audience);
      if (
        claims.iss !== GITHUB_OIDC_ISSUER ||
        !audienceMatches(claims.aud, this.config.audience) ||
        claims.sub !== this.config.subject ||
        stringClaim(claims, "repository") !== this.config.repository ||
        stringClaim(claims, "repository_id") !== this.config.repositoryId ||
        stringClaim(claims, "repository_owner_id") !==
          this.config.repositoryOwnerId ||
        stringClaim(claims, "workflow_ref") !== this.config.workflowRef ||
        stringClaim(claims, "ref") !== this.config.ref ||
        stringClaim(claims, "environment") !== this.config.environment ||
        stringClaim(claims, "event_name") !== this.config.eventName ||
        stringClaim(claims, "runner_environment") !== "github-hosted"
      ) {
        throw new DeploymentRegistryWriteError("invalid_attestation");
      }
      if (
        !FULL_SHA_RE.test(stringClaim(claims, "sha")) ||
        !NUMERIC_CLAIM_RE.test(stringClaim(claims, "actor_id")) ||
        !NUMERIC_CLAIM_RE.test(stringClaim(claims, "run_id")) ||
        !NUMERIC_CLAIM_RE.test(stringClaim(claims, "run_attempt")) ||
        stringClaim(claims, "jti").length > 256
      ) {
        throw new DeploymentRegistryWriteError("invalid_attestation");
      }

      const command = attestation.command;
      const environment = command.deployment.environment;
      const timestamp = attestationTimestamp(command);
      const runId = stringClaim(claims, "run_id");
      const runAttempt = stringClaim(claims, "run_attempt");
      if (
        environment !== this.config.environment ||
        command.deployment.service !== STAGING_SYNTHETIC_SERVICE ||
        command.deployment.deploymentId !==
          `github-${runId}-${runAttempt}` ||
        (command.action === "activate" &&
          command.deployment.deploymentSha !== stringClaim(claims, "sha")) ||
        Math.abs(this.now() - timestamp) > MAX_ATTESTATION_CLOCK_SKEW_MS
      ) {
        throw new DeploymentRegistryWriteError("invalid_attestation");
      }

      return command;
    } catch {
      // Never surface token contents, JOSE diagnostics, or remote JWKS bodies.
      throw new DeploymentRegistryWriteError("invalid_attestation");
    }
  }
}
