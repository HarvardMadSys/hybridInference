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
import validMetricThresholdFixture from "./fixtures/valid-metric-threshold-firing.json";
import validDependencyFixture from "./fixtures/valid-dependency-unavailable-firing.json";

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

  // The Python contract mirror parses these same two files, so a rule that
  // drifts between the two implementations fails in one of the suites rather
  // than at ingress, where a producer only learns a status code.
  it("accepts the shared gateway metric-threshold fixture", () => {
    const event = parseAlertEvent(validMetricThresholdFixture, { now: TEST_NOW });

    expect(event).toEqual({
      ...validMetricThresholdFixture,
      occurred_at: "2026-07-19T06:00:00.000Z",
    });
  });

  it("accepts the shared gateway dependency fixture", () => {
    const event = parseAlertEvent(validDependencyFixture, { now: TEST_NOW });

    expect(event).toEqual({
      ...validDependencyFixture,
      occurred_at: "2026-07-19T06:00:00.000Z",
    });
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

describe("gateway alert types (metric_threshold_breach, dependency_unavailable)", () => {
  function metricEvent(
    context: Record<string, unknown>,
  ): Record<string, unknown> {
    return {
      schema_version: 1,
      event_id: "gateway-metric-1",
      alert_type: "metric_threshold_breach",
      fingerprint: "gateway:failed_request_rate",
      status: "firing",
      severity: "error",
      title: "Failed-request rate exceeded",
      occurred_at: "2026-07-20T00:00:00Z",
      summary: "The failed-request rate crossed its configured threshold.",
      context,
      evidence_refs: [],
    };
  }

  it("accepts a numeric threshold breach with its optional counts", () => {
    const parsed = parseAlertEvent(
      metricEvent({
        metric: "failed_request_rate",
        observed: 0.123,
        threshold: 0.05,
        window_sec: 300,
        scope: "gateway",
        sample_count: 366,
      }),
      { now: TEST_NOW },
    );
    expect(parsed).toMatchObject({
      alert_type: "metric_threshold_breach",
      context: { metric: "failed_request_rate", observed: 0.123, threshold: 0.05 },
    });
  });

  // The migration must not make the auth-spike alert less actionable than the
  // one it replaces: today on-call reads the attacker addresses straight out
  // of the Slack message and blocks them. They survive, but as a typed field.
  it("keeps the addresses on-call acts on, in a field that only accepts addresses", () => {
    const parsed = parseAlertEvent(
      metricEvent({
        metric: "auth_failure_count",
        observed: 41,
        threshold: 20,
        window_sec: 300,
        source_addresses: ["203.0.113.7", "2001:db8::1"],
        distinct_sources: 9,
        top_source_share: 0.8,
      }),
      { now: TEST_NOW },
    );
    expect(parsed).toMatchObject({
      context: { source_addresses: ["203.0.113.7", "2001:db8::1"] },
    });

    // Typed, so it cannot become the free-text channel the old context was.
    for (const bad of [
      ["1.2.3.4 (12), 5.6.7.8 (3)"],
      ["not-an-address"],
      ["203.0.113.7; DROP TABLE"],
      ["203.0.113.7", "203.0.113.7"],
      [],
      ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", "5.5.5.5", "6.6.6.6"],
    ]) {
      expect(() =>
        parseAlertEvent(
          metricEvent({
            metric: "auth_failure_count",
            observed: 41,
            threshold: 20,
            source_addresses: bad,
          }),
          { now: TEST_NOW },
        ),
      ).toThrow(ValidationError);
    }
  });

  it("keeps the scoped subject but refuses key prefixes and free text", () => {
    expect(
      parseAlertEvent(
        metricEvent({
          metric: "user_daily_cost",
          observed: 42.5,
          threshold: 25,
          scope: "user",
          subject: "4711",
        }),
        { now: TEST_NOW },
      ),
    ).toMatchObject({ context: { scope: "user", subject: "4711" } });

    // Credential material and pre-formatted prose have no field to land in.
    for (const smuggled of [
      { top_key_prefixes: "hyi-abcdefghijklmnopqrstu (7)" },
      { rate: "12.3% (45 of 366 requests, last 300s)" },
      { top_paths: "/v1/chat/completions (12)" },
      { subject: "user 4711 (over budget)" },
    ]) {
      expect(() =>
        parseAlertEvent(
          metricEvent({
            metric: "user_daily_cost",
            observed: 42.5,
            threshold: 25,
            ...smuggled,
          }),
          { now: TEST_NOW },
        ),
      ).toThrow(ValidationError);
    }
  });

  it("rejects an unknown metric and an out-of-range share", () => {
    expect(() =>
      parseAlertEvent(
        metricEvent({ metric: "cpu_temperature", observed: 1, threshold: 0 }),
        { now: TEST_NOW },
      ),
    ).toThrow(ValidationError);
    expect(() =>
      parseAlertEvent(
        metricEvent({
          metric: "http_5xx_rate",
          observed: 1,
          threshold: 0,
          top_source_share: 1.5,
        }),
        { now: TEST_NOW },
      ),
    ).toThrow(ValidationError);
  });

  it("scopes the typed-IP exception to metrics where blocking is the response", () => {
    // Otherwise any producer could attach addresses to any alert and the
    // renderer would publish them — the exception has to be narrow.
    expect(() =>
      parseAlertEvent(
        metricEvent({
          metric: "user_daily_cost",
          observed: 42.5,
          threshold: 25,
          source_addresses: ["203.0.113.7"],
        }),
        { now: TEST_NOW },
      ),
    ).toThrow(ValidationError);
  });

  it("rejects a firing breach whose observed value is under its threshold", () => {
    // Such a card would argue against itself in Slack.
    expect(() =>
      parseAlertEvent(
        metricEvent({ metric: "http_5xx_rate", observed: 0.01, threshold: 0.05 }),
        { now: TEST_NOW },
      ),
    ).toThrow(ValidationError);
    // The same numbers are legitimate once the incident is resolving.
    expect(
      parseAlertEvent(
        {
          ...metricEvent({ metric: "http_5xx_rate", observed: 0.01, threshold: 0.05 }),
          status: "resolved",
          severity: "info",
        },
        { now: TEST_NOW },
      ),
    ).toMatchObject({ status: "resolved" });
  });

  it("accepts a dependency outage and rejects a connection string as backend", () => {
    const base = {
      schema_version: 1,
      event_id: "gateway-dependency-1",
      alert_type: "dependency_unavailable",
      fingerprint: "gateway:operational_store",
      status: "firing",
      severity: "critical",
      title: "Database disconnected",
      occurred_at: "2026-07-20T00:00:00Z",
      summary: "The operational store failed its health check.",
      evidence_refs: [],
    };
    expect(
      parseAlertEvent(
        {
          ...base,
          context: {
            dependency: "operational_store",
            backend: "postgres",
            reason: "health_check_failed",
          },
        },
        { now: TEST_NOW },
      ),
    ).toMatchObject({
      alert_type: "dependency_unavailable",
      context: {
        dependency: "operational_store",
        backend: "postgres",
        // Keeps the triage signal the backend sends today as free-text `error`.
        reason: "health_check_failed",
      },
    });

    // The failure cause is an enum, so the raw error string it replaces —
    // which can carry a DSN or host — has nowhere to land.
    expect(() =>
      parseAlertEvent(
        {
          ...base,
          context: {
            dependency: "operational_store",
            reason: "could not connect to postgres://db.internal:5432",
          },
        },
        { now: TEST_NOW },
      ),
    ).toThrow(ValidationError);

    // A DSN carries a host and credentials — untrustedString must refuse it.
    expect(() =>
      parseAlertEvent(
        {
          ...base,
          context: {
            dependency: "operational_store",
            // A credential-free DSN passes every untrustedString check, so
            // the field's shape has to be constrained, not just scanned.
            backend: "postgres://localhost/app",
          },
        },
        { now: TEST_NOW },
      ),
    ).toThrow(ValidationError);

    expect(() =>
      parseAlertEvent(
        { ...base, context: { dependency: "redis" } },
        { now: TEST_NOW },
      ),
    ).toThrow(ValidationError);
  });
});
