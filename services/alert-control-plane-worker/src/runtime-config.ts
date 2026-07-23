const MINIMUM_ROUTE_KEY_BYTES = 32;
const MAXIMUM_ROUTE_KEY_BYTES = 4_096;
const CHANNEL_ID_RE = /^[CGD][A-Z0-9]{1,255}$/;
const SINK_ID_RE = /^[a-z][a-z0-9._-]{0,63}$/;
const BOT_TOKEN_RE = /^xoxb-[A-Za-z0-9-]{10,1019}$/;

export interface RuntimeEnvironment {
  readonly CONTROL_PLANE_MODE?: string;
  readonly ROUTE_KEY_V1?: string;
  readonly SLACK_BOT_TOKEN?: string;
  readonly SLACK_CHANNEL_ID?: string;
  readonly SLACK_SINK_ID?: string;
  readonly PRINCIPAL_ACTIVE_LIMIT?: string;
  readonly QUOTA_PENDING_LEASE_MS?: string;
  readonly PRINCIPAL_QUOTAS?: DurableObjectNamespace;
}

export interface StagingRuntimeConfig {
  readonly mode: "staging-runtime";
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
  | "principal_quota_binding_missing";

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
  if (mode !== "staging-runtime") {
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

  return {
    mode: "staging-runtime",
    routeKey: routeMaterial,
    slack: { botToken, channelId, sinkId },
    quota: {
      activeLimit,
      pendingLeaseMs,
      namespace: env.PRINCIPAL_QUOTAS,
    },
  };
}
