const MINIMUM_ROUTE_KEY_BYTES = 32;
const MAXIMUM_ROUTE_KEY_BYTES = 4_096;
const CHANNEL_ID_RE = /^[CGD][A-Z0-9]{1,255}$/;
const SINK_ID_RE = /^[a-z][a-z0-9._-]{0,63}$/;
const BOT_TOKEN_RE = /^xoxb-[A-Za-z0-9-]{10,1019}$/;
const REPOSITORY_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const NUMERIC_ID_RE = /^[1-9][0-9]{0,31}$/;
const WORKFLOW_REF_RE =
  /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/\.github\/workflows\/[A-Za-z0-9_.-]+\.ya?ml@refs\/heads\/dev$/;

export const GITHUB_DEPLOYMENT_ATTESTATION_AUDIENCE =
  "alert-control-plane-deployment-attestation";

export interface RuntimeEnvironment {
  readonly CONTROL_PLANE_MODE?: string;
  readonly ROUTE_KEY_V1?: string;
  readonly SLACK_BOT_TOKEN?: string;
  readonly SLACK_CHANNEL_ID?: string;
  readonly SLACK_SINK_ID?: string;
  readonly PRINCIPAL_ACTIVE_LIMIT?: string;
  readonly QUOTA_PENDING_LEASE_MS?: string;
  readonly PRINCIPAL_QUOTAS?: DurableObjectNamespace;
  readonly DEPLOYMENT_REGISTRIES?: DurableObjectNamespace;
  readonly PRODUCER_TOKEN_SIGNING_KEY_V1?: string;
  readonly PRODUCER_TOKEN_TTL_SECONDS?: string;
  readonly GITHUB_OIDC_SUBJECT?: string;
  readonly GITHUB_OIDC_REPOSITORY?: string;
  readonly GITHUB_OIDC_REPOSITORY_ID?: string;
  readonly GITHUB_OIDC_REPOSITORY_OWNER_ID?: string;
  readonly GITHUB_OIDC_WORKFLOW_REF?: string;
  readonly GITHUB_OIDC_REF?: string;
  readonly GITHUB_OIDC_ENVIRONMENT?: string;
  readonly GITHUB_OIDC_EVENT_NAME?: string;
}

interface StagingRuntimeConfigBase {
  readonly routeKey: string;
  readonly slack: {
    readonly botToken: string;
    readonly channelId: string;
    readonly sinkId: string;
  };
  readonly quota: {
    readonly activeLimit: number;
    readonly pendingLeaseMs: number;
    readonly namespace: DurableObjectNamespace;
  };
}

export interface StagingRuntimeOnlyConfig extends StagingRuntimeConfigBase {
  readonly mode: "staging-runtime";
}

export interface StagingIngressConfig extends StagingRuntimeConfigBase {
  readonly mode: "staging-ingress";
  readonly identity: {
    readonly registryNamespace: DurableObjectNamespace;
    readonly producerTokenSigningKey: string;
    readonly producerTokenTtlSeconds: number;
    readonly githubOidc: {
      readonly audience: typeof GITHUB_DEPLOYMENT_ATTESTATION_AUDIENCE;
      readonly subject: string;
      readonly repository: string;
      readonly repositoryId: string;
      readonly repositoryOwnerId: string;
      readonly workflowRef: string;
      readonly ref: "refs/heads/dev";
      readonly environment: "staging";
      readonly eventName: "workflow_dispatch";
    };
  };
}

export type StagingRuntimeConfig =
  | StagingRuntimeOnlyConfig
  | StagingIngressConfig;

export type RuntimeConfigResult =
  | { readonly mode: "dormant" }
  | { readonly mode: "invalid"; readonly errorCode: RuntimeConfigErrorCode }
  | StagingRuntimeConfig;

export type RuntimeConfigErrorCode =
  | "control_plane_mode_invalid"
  | "route_key_missing"
  | "route_key_invalid"
  | "slack_bot_token_missing"
  | "slack_bot_token_invalid"
  | "slack_channel_id_missing"
  | "slack_channel_id_invalid"
  | "slack_sink_id_missing"
  | "slack_sink_id_invalid"
  | "principal_active_limit_missing"
  | "principal_active_limit_invalid"
  | "quota_pending_lease_ms_missing"
  | "quota_pending_lease_ms_invalid"
  | "principal_quota_binding_missing"
  | "deployment_registry_binding_missing"
  | "producer_token_signing_key_missing"
  | "producer_token_signing_key_invalid"
  | "producer_token_ttl_seconds_missing"
  | "producer_token_ttl_seconds_invalid"
  | "github_oidc_subject_missing"
  | "github_oidc_subject_invalid"
  | "github_oidc_repository_missing"
  | "github_oidc_repository_invalid"
  | "github_oidc_repository_id_missing"
  | "github_oidc_repository_id_invalid"
  | "github_oidc_repository_owner_id_missing"
  | "github_oidc_repository_owner_id_invalid"
  | "github_oidc_workflow_ref_missing"
  | "github_oidc_workflow_ref_invalid"
  | "github_oidc_ref_missing"
  | "github_oidc_ref_invalid"
  | "github_oidc_environment_missing"
  | "github_oidc_environment_invalid"
  | "github_oidc_event_name_missing"
  | "github_oidc_event_name_invalid";

function present(value: string | undefined): value is string {
  return value !== undefined && value.length > 0;
}

function parseInteger(
  value: string | undefined,
  minimum: number,
  maximum: number,
): number | null {
  if (value === undefined || !/^[1-9][0-9]*$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum
    ? parsed
    : null;
}

function isDurableObjectNamespace(
  value: DurableObjectNamespace | undefined,
): value is DurableObjectNamespace {
  if (value === undefined || value === null || typeof value !== "object") {
    return false;
  }
  const candidate = value as unknown as Record<string, unknown>;
  return (
    typeof candidate.idFromName === "function" &&
    typeof candidate.get === "function"
  );
}

/**
 * Parse the explicit Phase C1 activation gate without ever embedding a binding
 * value in an error. Missing mode remains fully dormant; partial or malformed
 * active configuration fails closed.
 */
export function parseRuntimeConfig(
  env: RuntimeEnvironment | undefined,
): RuntimeConfigResult {
  const mode = env?.CONTROL_PLANE_MODE;
  if (mode === undefined || mode === "dormant") return { mode: "dormant" };
  if (mode !== "staging-runtime" && mode !== "staging-ingress") {
    return { mode: "invalid", errorCode: "control_plane_mode_invalid" };
  }
  if (env === undefined) return { mode: "dormant" };

  const routeMaterial = env.ROUTE_KEY_V1;
  if (!present(routeMaterial)) {
    return { mode: "invalid", errorCode: "route_key_missing" };
  }
  const routeKeyBytes = new TextEncoder().encode(routeMaterial).byteLength;
  if (
    routeKeyBytes < MINIMUM_ROUTE_KEY_BYTES ||
    routeKeyBytes > MAXIMUM_ROUTE_KEY_BYTES
  ) {
    return { mode: "invalid", errorCode: "route_key_invalid" };
  }

  const botToken = env.SLACK_BOT_TOKEN;
  if (!present(botToken)) {
    return { mode: "invalid", errorCode: "slack_bot_token_missing" };
  }
  if (!BOT_TOKEN_RE.test(botToken)) {
    return { mode: "invalid", errorCode: "slack_bot_token_invalid" };
  }

  const channelId = env.SLACK_CHANNEL_ID;
  if (!present(channelId)) {
    return { mode: "invalid", errorCode: "slack_channel_id_missing" };
  }
  if (!CHANNEL_ID_RE.test(channelId)) {
    return { mode: "invalid", errorCode: "slack_channel_id_invalid" };
  }

  const sinkId = env.SLACK_SINK_ID;
  if (!present(sinkId)) {
    return { mode: "invalid", errorCode: "slack_sink_id_missing" };
  }
  if (!SINK_ID_RE.test(sinkId)) {
    return { mode: "invalid", errorCode: "slack_sink_id_invalid" };
  }

  if (env.PRINCIPAL_ACTIVE_LIMIT === undefined) {
    return {
      mode: "invalid",
      errorCode: "principal_active_limit_missing",
    };
  }
  const activeLimit = parseInteger(env.PRINCIPAL_ACTIVE_LIMIT, 1, 1_000);
  if (activeLimit === null) {
    return {
      mode: "invalid",
      errorCode: "principal_active_limit_invalid",
    };
  }

  if (env.QUOTA_PENDING_LEASE_MS === undefined) {
    return {
      mode: "invalid",
      errorCode: "quota_pending_lease_ms_missing",
    };
  }
  const pendingLeaseMs = parseInteger(
    env.QUOTA_PENDING_LEASE_MS,
    30_000,
    15 * 60_000,
  );
  if (pendingLeaseMs === null) {
    return {
      mode: "invalid",
      errorCode: "quota_pending_lease_ms_invalid",
    };
  }

  if (!isDurableObjectNamespace(env.PRINCIPAL_QUOTAS)) {
    return {
      mode: "invalid",
      errorCode: "principal_quota_binding_missing",
    };
  }

  const activeBase: StagingRuntimeConfigBase = {
    routeKey: routeMaterial,
    slack: { botToken, channelId, sinkId },
    quota: {
      activeLimit,
      pendingLeaseMs,
      namespace: env.PRINCIPAL_QUOTAS,
    },
  };

  if (mode === "staging-runtime") {
    return { mode, ...activeBase };
  }

  if (!isDurableObjectNamespace(env.DEPLOYMENT_REGISTRIES)) {
    return {
      mode: "invalid",
      errorCode: "deployment_registry_binding_missing",
    };
  }

  const producerTokenSigningKey = env.PRODUCER_TOKEN_SIGNING_KEY_V1;
  if (!present(producerTokenSigningKey)) {
    return {
      mode: "invalid",
      errorCode: "producer_token_signing_key_missing",
    };
  }
  const producerSigningKeyBytes = new TextEncoder().encode(
    producerTokenSigningKey,
  ).byteLength;
  if (
    producerSigningKeyBytes < MINIMUM_ROUTE_KEY_BYTES ||
    producerSigningKeyBytes > MAXIMUM_ROUTE_KEY_BYTES
  ) {
    return {
      mode: "invalid",
      errorCode: "producer_token_signing_key_invalid",
    };
  }

  if (env.PRODUCER_TOKEN_TTL_SECONDS === undefined) {
    return {
      mode: "invalid",
      errorCode: "producer_token_ttl_seconds_missing",
    };
  }
  const producerTokenTtlSeconds = parseInteger(
    env.PRODUCER_TOKEN_TTL_SECONDS,
    60,
    60 * 60,
  );
  if (producerTokenTtlSeconds === null) {
    return {
      mode: "invalid",
      errorCode: "producer_token_ttl_seconds_invalid",
    };
  }

  const subject = env.GITHUB_OIDC_SUBJECT;
  if (!present(subject)) {
    return { mode: "invalid", errorCode: "github_oidc_subject_missing" };
  }
  if (
    subject.length > 512 ||
    !subject.endsWith(":environment:staging") ||
    /[\u0000-\u001f\u007f]/u.test(subject)
  ) {
    return { mode: "invalid", errorCode: "github_oidc_subject_invalid" };
  }

  const repository = env.GITHUB_OIDC_REPOSITORY;
  if (!present(repository)) {
    return { mode: "invalid", errorCode: "github_oidc_repository_missing" };
  }
  if (!REPOSITORY_RE.test(repository)) {
    return { mode: "invalid", errorCode: "github_oidc_repository_invalid" };
  }

  const repositoryId = env.GITHUB_OIDC_REPOSITORY_ID;
  if (!present(repositoryId)) {
    return {
      mode: "invalid",
      errorCode: "github_oidc_repository_id_missing",
    };
  }
  if (!NUMERIC_ID_RE.test(repositoryId)) {
    return {
      mode: "invalid",
      errorCode: "github_oidc_repository_id_invalid",
    };
  }

  const repositoryOwnerId = env.GITHUB_OIDC_REPOSITORY_OWNER_ID;
  if (!present(repositoryOwnerId)) {
    return {
      mode: "invalid",
      errorCode: "github_oidc_repository_owner_id_missing",
    };
  }
  if (!NUMERIC_ID_RE.test(repositoryOwnerId)) {
    return {
      mode: "invalid",
      errorCode: "github_oidc_repository_owner_id_invalid",
    };
  }

  const workflowRef = env.GITHUB_OIDC_WORKFLOW_REF;
  if (!present(workflowRef)) {
    return {
      mode: "invalid",
      errorCode: "github_oidc_workflow_ref_missing",
    };
  }
  if (
    !WORKFLOW_REF_RE.test(workflowRef) ||
    !workflowRef.startsWith(`${repository}/.github/workflows/`)
  ) {
    return {
      mode: "invalid",
      errorCode: "github_oidc_workflow_ref_invalid",
    };
  }

  if (env.GITHUB_OIDC_REF === undefined) {
    return { mode: "invalid", errorCode: "github_oidc_ref_missing" };
  }
  if (env.GITHUB_OIDC_REF !== "refs/heads/dev") {
    return { mode: "invalid", errorCode: "github_oidc_ref_invalid" };
  }

  if (env.GITHUB_OIDC_ENVIRONMENT === undefined) {
    return {
      mode: "invalid",
      errorCode: "github_oidc_environment_missing",
    };
  }
  if (env.GITHUB_OIDC_ENVIRONMENT !== "staging") {
    return {
      mode: "invalid",
      errorCode: "github_oidc_environment_invalid",
    };
  }

  if (env.GITHUB_OIDC_EVENT_NAME === undefined) {
    return {
      mode: "invalid",
      errorCode: "github_oidc_event_name_missing",
    };
  }
  if (env.GITHUB_OIDC_EVENT_NAME !== "workflow_dispatch") {
    return {
      mode: "invalid",
      errorCode: "github_oidc_event_name_invalid",
    };
  }

  return {
    mode,
    ...activeBase,
    identity: {
      registryNamespace: env.DEPLOYMENT_REGISTRIES,
      producerTokenSigningKey,
      producerTokenTtlSeconds,
      githubOidc: {
        audience: GITHUB_DEPLOYMENT_ATTESTATION_AUDIENCE,
        subject,
        repository,
        repositoryId,
        repositoryOwnerId,
        workflowRef,
        ref: "refs/heads/dev",
        environment: "staging",
        eventName: "workflow_dispatch",
      },
    },
  };
}
