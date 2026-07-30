import { describe, expect, it } from "vitest";

import invalidContextTypeFixture from "./fixtures/invalid-context-type.json";
import invalidPathFixture from "./fixtures/invalid-evidence-path-traversal.json";
import invalidIpv6Fixture from "./fixtures/invalid-network-ipv6.json";
import invalidPromptFixture from "./fixtures/invalid-prompt-injection.json";
import invalidSecretFixture from "./fixtures/invalid-secret-material.json";
import invalidTrustedFixture from "./fixtures/invalid-trusted-field.json";
import invalidAlertTypeFixture from "./fixtures/invalid-unsupported-alert-type.json";
import invalidContextFixture from "./fixtures/invalid-unknown-context-key.json";
import validFiringFixture from "./fixtures/valid-provider-circuit-firing.json";
import validResolvedFixture from "./fixtures/valid-provider-circuit-resolved.json";
import validModelFiringFixture from "./fixtures/valid-model-unavailable-firing.json";
import validModelResolvedFixture from "./fixtures/valid-model-unavailable-resolved.json";

import {
  canonicalEventDigest,
  canonicalJson,
  createCanonicalEnvelope,
  parseAlertEvent,
  parseTrustedMetadata,
  ValidationError,
} from "../src/validation";

const TEST_NOW = Date.parse("2026-07-20T00:00:00Z");

function copyFixture(value: unknown): Record<string, unknown> {
  return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
}

function validEvent(): Record<string, unknown> {
  return copyFixture(validFiringFixture);
}

function validTrusted(): Record<string, unknown> {
  return {
    environment: "staging",
    source: "gateway",
    principal: "staging-gateway",
    deployment_id: "gateway-20260719-1",
    deployment_sha: "A".repeat(40),
    artifact_digest: `sha256:${"B".repeat(64)}`,
    registry_version: 7,
  };
}

describe("canonical AlertEvent validation", () => {
  it("accepts the shared firing and resolved fixtures", () => {
    const firing = parseAlertEvent(validFiringFixture, { now: TEST_NOW });
    const resolved = parseAlertEvent(validResolvedFixture, { now: TEST_NOW });

    expect(firing).toMatchObject({
      schema_version: 1,
      alert_type: "provider_circuit_open",
      occurred_at: "2026-07-19T06:00:00.000Z",
      context: {
        provider: "diffusiongemma:local-8002",
        availability: 0,
        affected_users: 55,
      },
    });
    expect(resolved).toMatchObject({
      status: "resolved",
      context: { final_failure_count: 8, outage_duration_ms: 252_000 },
    });
  });

  it("accepts platform-neutral model unavailability and recovery fixtures", () => {
    const firing = parseAlertEvent(validModelFiringFixture, { now: TEST_NOW });
    const resolved = parseAlertEvent(validModelResolvedFixture, { now: TEST_NOW });

    expect(firing).toEqual({
      ...validModelFiringFixture,
      occurred_at: "2026-07-19T06:00:00.000Z",
    });
    expect(resolved).toEqual({
      ...validModelResolvedFixture,
      occurred_at: "2026-07-19T06:20:00.000Z",
    });
    expect(firing).not.toHaveProperty("slack_text");
    expect(firing).not.toHaveProperty("environment");
    expect(firing).not.toHaveProperty("source");
  });

  it.each([
    "environment",
    "source",
    "principal",
    "deployment_id",
    "deployment_sha",
    "artifact_digest",
    "registry_version",
    "slack_text",
    "slack_blocks",
    "slack_channel_id",
    "thread_ts",
    "trusted",
  ])("rejects producer-supplied trusted or Slack-owned field %s", (fieldName) => {
    expect(() =>
      parseAlertEvent({ ...validEvent(), [fieldName]: "producer-value" }, { now: TEST_NOW }),
    ).toThrow(/trusted or Slack-owned field/);
  });

  it("rejects the shared invalid trusted-field fixture", () => {
    expect(() => parseAlertEvent(invalidTrustedFixture, { now: TEST_NOW })).toThrow(
      /environment/,
    );
  });

  it("rejects unknown alert types and makes registry extension explicit", () => {
    expect(() =>
      parseAlertEvent({ ...validEvent(), alert_type: "model_probe_failed" }, { now: TEST_NOW }),
    ).toThrow(/alert_type is unsupported/);
    expect(() => parseAlertEvent(invalidAlertTypeFixture, { now: TEST_NOW })).toThrow(
      /alert_type is unsupported/,
    );
  });

  it("rejects missing, legacy, and extra top-level fields", () => {
    const missing = validEvent();
    delete missing.event_id;
    expect(() => parseAlertEvent(missing, { now: TEST_NOW })).toThrow(/event_id/);
    expect(() =>
      parseAlertEvent({ ...validEvent(), schema_version: "1" }, { now: TEST_NOW }),
    ).toThrow(/schema_version/);
    expect(() =>
      parseAlertEvent({ ...validEvent(), version: "2" }, { now: TEST_NOW }),
    ).toThrow(/unsupported field: version/);
    expect(() =>
      parseAlertEvent({ ...validEvent(), alert_id: "legacy" }, { now: TEST_NOW }),
    ).toThrow(/unsupported field: alert_id/);
  });

  it("enforces the provider context key, type, enum, and range allowlist", () => {
    expect(() => parseAlertEvent(invalidContextFixture, { now: TEST_NOW })).toThrow(
      /unsupported field: offending_users/,
    );
    expect(() => parseAlertEvent(invalidContextTypeFixture, { now: TEST_NOW })).toThrow(
      /finite number/,
    );

    const cases: Array<[Record<string, unknown>, RegExp]> = [
      [{ provider: "openai", unknown: true }, /unsupported field/],
      [{ provider: "openai", availability: "0.5" }, /finite number/],
      [{ provider: "openai", availability: 1.1 }, /between 0 and 1/],
      [{ provider: "openai", affected_users: 1.5 }, /integer/],
      [{ provider: "openai", reason: "producer-defined" }, /reason is invalid/],
      [{ availability: 0 }, /context.provider/],
    ];
    for (const [context, message] of cases) {
      expect(() => parseAlertEvent({ ...validEvent(), context }, { now: TEST_NOW })).toThrow(
        message,
      );
    }
  });

  it("enforces a separate model-unavailable context allowlist", () => {
    const base = copyFixture(validModelFiringFixture);
    const cases: Array<[Record<string, unknown>, RegExp]> = [
      [{ model_id: "deepseek-v3", provider: "openai" }, /unsupported field: provider/],
      [{ failure_threshold: 2 }, /context.model_id/],
      [{ model_id: "deepseek-v3", failure_threshold: 0 }, /between 1/],
      [{ model_id: "deepseek-v3", consecutive_failures: 1.5 }, /integer/],
      [{ model_id: "deepseek-v3", reason: "connection_refused" }, /reason is invalid/],
      [{ model_id: "deepseek-v3" }, /requires consecutive_failures/],
    ];
    for (const [context, message] of cases) {
      expect(() => parseAlertEvent({ ...base, context }, { now: TEST_NOW })).toThrow(message);
    }
    expect(() =>
      parseAlertEvent(
        {
          ...copyFixture(validModelResolvedFixture),
          context: { model_id: "deepseek-v3", failure_threshold: 2 },
        },
        { now: TEST_NOW },
      ),
    ).toThrow(/must not contain firing-only fields/);
  });

  it.each([
    "Bearer abcdefghijklmnop",
    "api_key=abcdefghi",
    "hyi-abcdefghijklmnopqrstuvwxyz",
    "ghp_abcdefghijklmnopqrstuvwxyz1234",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "sk-test-NOTAREAL",
    "gsk_NOTAREAL",
    "xai-NOTAREAL",
    "rk_NOTAREAL",
    "AIzaNOTAREAL00",
    "-----BEGIN PRIVATE KEY-----",
    "https://hooks.slack.com/services/TEST/TEST/TEST",
  ])("rejects secret material before persistence: %s", (secret) => {
    expect(() =>
      parseAlertEvent({ ...validEvent(), summary: `failure ${secret}` }, { now: TEST_NOW }),
    ).toThrow(/secret material/);
  });

  it("rejects the shared secret-material fixture", () => {
    expect(() => parseAlertEvent(invalidSecretFixture, { now: TEST_NOW })).toThrow(
      /secret material/,
    );
  });

  it("rejects user/network identifiers, controls, and prompt instructions", () => {
    expect(() => parseAlertEvent(invalidPromptFixture, { now: TEST_NOW })).toThrow(
      /control instructions/,
    );
    expect(() =>
      parseAlertEvent(
        { ...validEvent(), context: { provider: "openai", error: "owner@example.com" } },
        { now: TEST_NOW },
      ),
    ).toThrow(/user or network identifier/);
    expect(() =>
      parseAlertEvent(
        { ...validEvent(), context: { provider: "openai", error: "connect 192.0.2.1" } },
        { now: TEST_NOW },
      ),
    ).toThrow(/user or network identifier/);
    expect(() => parseAlertEvent(invalidIpv6Fixture, { now: TEST_NOW })).toThrow(
      /user or network identifier/,
    );
    expect(() =>
      parseAlertEvent({ ...validEvent(), title: "Provider\u0000 down" }, { now: TEST_NOW }),
    ).toThrow(/control character/);
  });

  it.each([
    "../config/models.yaml",
    "config/../../etc/passwd",
    "/etc/passwd",
    "C:\\Windows\\system.ini",
    "https://example.com/evidence",
    "config/%2e%2e/secrets",
    "config//models.yaml",
    "config/./models.yaml",
    "~/.ssh/id_rsa",
  ])("rejects non-canonical evidence path %s", (reference) => {
    expect(() =>
      parseAlertEvent({ ...validEvent(), evidence_refs: [reference] }, { now: TEST_NOW }),
    ).toThrow(/repository-relative path|path traversal/);
  });

  it("rejects the shared traversal fixture and duplicate evidence paths", () => {
    expect(() => parseAlertEvent(invalidPathFixture, { now: TEST_NOW })).toThrow(/path traversal/);
    expect(() =>
      parseAlertEvent(
        { ...validEvent(), evidence_refs: ["config/models.yaml", "config/models.yaml"] },
        { now: TEST_NOW },
      ),
    ).toThrow(/duplicates/);
  });

  it("normalizes Unicode to NFC and enforces the 512-character fingerprint boundary", () => {
    const decomposed = parseAlertEvent(
      {
        ...validEvent(),
        fingerprint: "provider:e\u0301",
        context: { provider: "e\u0301" },
      },
      { now: TEST_NOW },
    );
    expect(decomposed.fingerprint).toBe("provider:é");
    expect(decomposed.alert_type).toBe("provider_circuit_open");
    if (decomposed.alert_type !== "provider_circuit_open") {
      throw new Error("expected provider circuit fixture");
    }
    expect(decomposed.context.provider).toBe("é");
    expect(
      parseAlertEvent({ ...validEvent(), fingerprint: "界".repeat(512) }, { now: TEST_NOW })
        .fingerprint,
    ).toHaveLength(512);
    expect(() =>
      parseAlertEvent({ ...validEvent(), fingerprint: "界".repeat(513) }, { now: TEST_NOW }),
    ).toThrow(/512 characters/);
  });

  it("validates real calendar timestamps and bounds future skew", () => {
    expect(() =>
      parseAlertEvent({ ...validEvent(), occurred_at: "2026-02-30T00:00:00Z" }, { now: TEST_NOW }),
    ).toThrow(/ISO timestamp/);
    expect(() =>
      parseAlertEvent(
        { ...validEvent(), occurred_at: "2026-07-20T00:05:01Z" },
        { now: TEST_NOW, maxFutureSkewMs: 300_000 },
      ),
    ).toThrow(/future/);
  });
});

describe("trusted envelope and canonical digest", () => {
  it("injects separately validated trusted deployment metadata", () => {
    const trusted = parseTrustedMetadata(validTrusted());
    const envelope = createCanonicalEnvelope(validEvent(), validTrusted(), { now: TEST_NOW });

    expect(trusted).toMatchObject({
      environment: "staging",
      source: "gateway",
      deployment_sha: "a".repeat(40),
      artifact_digest: `sha256:${"b".repeat(64)}`,
      registry_version: 7,
    });
    expect(envelope.event).not.toHaveProperty("environment");
    expect(envelope.trusted.environment).toBe("staging");
  });

  it("rejects malformed or incomplete trusted metadata", () => {
    expect(() => parseTrustedMetadata({ ...validTrusted(), environment: "local" })).toThrow(
      /environment/,
    );
    expect(() => parseTrustedMetadata({ ...validTrusted(), deployment_sha: "abc" })).toThrow(
      /deployment_sha/,
    );
    expect(() => parseTrustedMetadata({ ...validTrusted(), registry_version: 0 })).toThrow(
      /registry_version/,
    );
  });

  it("sorts object keys and produces a stable, content-sensitive SHA-256 digest", async () => {
    expect(canonicalJson({ z: 1, a: { y: 2, x: 3 } })).toBe(
      '{"a":{"x":3,"y":2},"z":1}',
    );
    const first = parseAlertEvent(validEvent(), { now: TEST_NOW });
    const reorderedInput = validEvent();
    reorderedInput.context = {
      reason: "connection_refused",
      consecutive_failures: 8,
      affected_users: 55,
      error: "Connection refused",
      availability: 0,
      provider: "diffusiongemma:local-8002",
    };
    const reordered = parseAlertEvent(reorderedInput, { now: TEST_NOW });
    const changed = { ...first, event_id: "018f-provider-open-0002" };

    await expect(canonicalEventDigest(first)).resolves.toBe(await canonicalEventDigest(reordered));
    await expect(canonicalEventDigest(first)).resolves.not.toBe(
      await canonicalEventDigest(changed),
    );
    expect(await canonicalEventDigest(first)).toBe(
      "sha256:160f80cd70f0c8f241ebc1541771896d3f026fae8789e76c3d8f1a6277b39561",
    );
  });

  it("rejects unsupported values instead of silently omitting them from canonical JSON", () => {
    expect(() => canonicalJson({ valid: true, omitted: undefined })).toThrow(ValidationError);
  });
});
