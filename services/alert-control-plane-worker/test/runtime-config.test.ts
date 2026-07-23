import { describe, expect, it } from "vitest";

import {
  parseRuntimeConfig,
  type RuntimeConfigErrorCode,
  type RuntimeEnvironment,
} from "../src/runtime-config";

function namespace(): DurableObjectNamespace {
  return {
    idFromName() {
      return {} as DurableObjectId;
    },
    get() {
      return {} as DurableObjectStub;
    },
  } as unknown as DurableObjectNamespace;
}

function validEnvironment(
  overrides: Partial<RuntimeEnvironment> = {},
): RuntimeEnvironment {
  return {
    CONTROL_PLANE_MODE: "staging-runtime",
    ROUTE_KEY_V1: "runtime-test-route-key-material-32-bytes-minimum",
    SLACK_BOT_TOKEN: "xoxb-unit-test-token-123456",
    SLACK_CHANNEL_ID: "C123",
    SLACK_SINK_ID: "slack-staging",
    PRINCIPAL_ACTIVE_LIMIT: "10",
    QUOTA_PENDING_LEASE_MS: "120000",
    PRINCIPAL_QUOTAS: namespace(),
    ...overrides,
  };
}

function validIngressEnvironment(
  overrides: Partial<RuntimeEnvironment> = {},
): RuntimeEnvironment {
  return validEnvironment({
    CONTROL_PLANE_MODE: "staging-ingress",
    DEPLOYMENT_REGISTRIES: namespace(),
    PRODUCER_TOKEN_SIGNING_KEY_V1:
      "producer-signing-key-material-with-at-least-32-bytes",
    PRODUCER_TOKEN_TTL_SECONDS: "900",
    GITHUB_OIDC_SUBJECT:
      "repo:HarvardMadSys/hybridInference:environment:staging",
    GITHUB_OIDC_REPOSITORY: "HarvardMadSys/hybridInference",
    GITHUB_OIDC_REPOSITORY_ID: "123",
    GITHUB_OIDC_REPOSITORY_OWNER_ID: "456",
    GITHUB_OIDC_WORKFLOW_REF:
      "HarvardMadSys/hybridInference/.github/workflows/alert-control-plane-staging-lifecycle.yml@refs/heads/dev",
    GITHUB_OIDC_REF: "refs/heads/dev",
    GITHUB_OIDC_ENVIRONMENT: "staging",
    GITHUB_OIDC_EVENT_NAME: "workflow_dispatch",
    ...overrides,
  });
}

describe("Phase C1/C2 runtime configuration", () => {
  it("remains dormant unless the explicit staging gate is set", () => {
    expect(parseRuntimeConfig(undefined)).toEqual({ mode: "dormant" });
    expect(
      parseRuntimeConfig({
        CONTROL_PLANE_MODE: "dormant",
        SLACK_BOT_TOKEN: "partial-value-is-ignored",
      }),
    ).toEqual({ mode: "dormant" });
  });

  it("returns only validated, typed staging configuration", () => {
    const result = parseRuntimeConfig(validEnvironment());
    expect(result).toMatchObject({
      mode: "staging-runtime",
      slack: {
        channelId: "C123",
        sinkId: "slack-staging",
      },
      quota: {
        activeLimit: 10,
        pendingLeaseMs: 120_000,
      },
    });
  });

  it("opens C2 only when every identity binding and allowlist is valid", () => {
    expect(parseRuntimeConfig(validIngressEnvironment())).toMatchObject({
      mode: "staging-ingress",
      identity: {
        producerTokenTtlSeconds: 900,
        githubOidc: {
          audience: "alert-control-plane-deployment-attestation",
          repository: "HarvardMadSys/hybridInference",
          ref: "refs/heads/dev",
          environment: "staging",
          eventName: "workflow_dispatch",
        },
      },
    });
  });

  it.each([
    [{ CONTROL_PLANE_MODE: "production" }, "control_plane_mode_invalid"],
    [{ ROUTE_KEY_V1: undefined }, "route_key_missing"],
    [{ ROUTE_KEY_V1: "short" }, "route_key_invalid"],
    [{ SLACK_BOT_TOKEN: undefined }, "slack_bot_token_missing"],
    [{ SLACK_BOT_TOKEN: "https://hooks.slack.test/secret" }, "slack_bot_token_invalid"],
    [{ SLACK_CHANNEL_ID: undefined }, "slack_channel_id_missing"],
    [{ SLACK_CHANNEL_ID: "general" }, "slack_channel_id_invalid"],
    [{ SLACK_SINK_ID: undefined }, "slack_sink_id_missing"],
    [{ SLACK_SINK_ID: "Slack Staging" }, "slack_sink_id_invalid"],
    [{ PRINCIPAL_ACTIVE_LIMIT: undefined }, "principal_active_limit_missing"],
    [{ PRINCIPAL_ACTIVE_LIMIT: "0" }, "principal_active_limit_invalid"],
    [{ QUOTA_PENDING_LEASE_MS: undefined }, "quota_pending_lease_ms_missing"],
    [{ QUOTA_PENDING_LEASE_MS: "29999" }, "quota_pending_lease_ms_invalid"],
    [{ PRINCIPAL_QUOTAS: undefined }, "principal_quota_binding_missing"],
  ] as const)(
    "fails closed for malformed active configuration %#",
    (override, errorCode: RuntimeConfigErrorCode) => {
      expect(parseRuntimeConfig(validEnvironment(override))).toEqual({
        mode: "invalid",
        errorCode,
      });
    },
  );

  it.each([
    [{ DEPLOYMENT_REGISTRIES: undefined }, "deployment_registry_binding_missing"],
    [{ PRODUCER_TOKEN_SIGNING_KEY_V1: undefined }, "producer_token_signing_key_missing"],
    [{ PRODUCER_TOKEN_SIGNING_KEY_V1: "short" }, "producer_token_signing_key_invalid"],
    [{ PRODUCER_TOKEN_TTL_SECONDS: undefined }, "producer_token_ttl_seconds_missing"],
    [{ PRODUCER_TOKEN_TTL_SECONDS: "3601" }, "producer_token_ttl_seconds_invalid"],
    [{ GITHUB_OIDC_SUBJECT: undefined }, "github_oidc_subject_missing"],
    [{ GITHUB_OIDC_SUBJECT: "repo:x/y:ref:refs/heads/dev" }, "github_oidc_subject_invalid"],
    [{ GITHUB_OIDC_REPOSITORY: "invalid" }, "github_oidc_repository_invalid"],
    [{ GITHUB_OIDC_REPOSITORY_ID: "repo" }, "github_oidc_repository_id_invalid"],
    [{ GITHUB_OIDC_REPOSITORY_OWNER_ID: "owner" }, "github_oidc_repository_owner_id_invalid"],
    [{ GITHUB_OIDC_WORKFLOW_REF: "invalid" }, "github_oidc_workflow_ref_invalid"],
    [{ GITHUB_OIDC_REF: "refs/heads/main" }, "github_oidc_ref_invalid"],
    [{ GITHUB_OIDC_ENVIRONMENT: "production" }, "github_oidc_environment_invalid"],
    [{ GITHUB_OIDC_EVENT_NAME: "pull_request" }, "github_oidc_event_name_invalid"],
  ] as const)(
    "fails closed for malformed C2 identity configuration %#",
    (override, errorCode: RuntimeConfigErrorCode) => {
      expect(parseRuntimeConfig(validIngressEnvironment(override))).toEqual({
        mode: "invalid",
        errorCode,
      });
    },
  );

  it("never copies a credential into a configuration error", () => {
    const token = "xoxb-sensitive-token-value-123456";
    const result = parseRuntimeConfig(
      validEnvironment({
        SLACK_BOT_TOKEN: token,
        SLACK_CHANNEL_ID: "invalid channel",
      }),
    );
    expect(JSON.stringify(result)).not.toContain(token);
    expect(result).toEqual({
      mode: "invalid",
      errorCode: "slack_channel_id_invalid",
    });
  });
});
