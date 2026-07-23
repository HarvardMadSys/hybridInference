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

describe("Phase C1 runtime configuration", () => {
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
