import { describe, expect, it } from "vitest";

import {
  deriveDeploymentRegistryRouteName,
  deriveIncidentRouteName,
  derivePrincipalQuotaRouteName,
  encodeIncidentRouteMaterial,
  routeNameForEnvelope,
} from "../src/routing";
import { createCanonicalEnvelope } from "../src/validation";

const ROUTE_KEY = "phase-1-test-route-key-material-32-bytes-minimum";

describe("incident route material", () => {
  it("uses unambiguous uint32 big-endian UTF-8 length prefixes", () => {
    const material = encodeIncidentRouteMaterial({
      environment: "staging",
      principal: "a|b",
      fingerprint: "界",
    });
    const view = new DataView(material.buffer, material.byteOffset, material.byteLength);

    expect(view.getUint32(0, false)).toBe(7);
    expect(new TextDecoder().decode(material.slice(4, 11))).toBe("staging");
    expect(view.getUint32(11, false)).toBe(3);
    expect(new TextDecoder().decode(material.slice(15, 18))).toBe("a|b");
    expect(view.getUint32(18, false)).toBe(3);
    expect(new TextDecoder().decode(material.slice(22))).toBe("界");
  });

  it("normalizes canonically equivalent Unicode before encoding", () => {
    const composed = encodeIncidentRouteMaterial({
      environment: "staging",
      principal: "gateway-é",
      fingerprint: "provider-é",
    });
    const decomposed = encodeIncidentRouteMaterial({
      environment: "staging",
      principal: "gateway-e\u0301",
      fingerprint: "provider-e\u0301",
    });
    expect(decomposed).toEqual(composed);
  });
});

describe("incident route HMAC", () => {
  it("is stable, opaque, URL-safe, and content-sensitive", async () => {
    const identity = {
      environment: "staging" as const,
      principal: "staging-gateway",
      fingerprint: "provider-circuit:openai",
    };
    const first = await deriveIncidentRouteName(ROUTE_KEY, identity);
    const second = await deriveIncidentRouteName(ROUTE_KEY, identity);
    const changed = await deriveIncidentRouteName(ROUTE_KEY, {
      ...identity,
      environment: "production",
    });

    expect(first).toBe(second);
    expect(first).toBe("_olsuVFvRVsLhS8rAjTgsRn-Fiiz2E9Bcfp5W_VyIxc");
    expect(first).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(first).not.toContain(identity.principal);
    expect(first).not.toContain(identity.fingerprint);
    expect(changed).not.toBe(first);
  });

  it("does not collide when components contain separator-like text", async () => {
    const left = await deriveIncidentRouteName(ROUTE_KEY, {
      environment: "staging",
      principal: "a|b",
      fingerprint: "c",
    });
    const right = await deriveIncidentRouteName(ROUTE_KEY, {
      environment: "staging",
      principal: "a",
      fingerprint: "b|c",
    });
    expect(left).not.toBe(right);
  });

  it("requires an operationally strong HMAC key", async () => {
    await expect(
      deriveIncidentRouteName("short-test-key", {
        environment: "staging",
        principal: "gateway",
        fingerprint: "provider",
      }),
    ).rejects.toThrow(/at least 32 bytes/);
  });

  it("uses a separate opaque domain for principal quota objects", async () => {
    const identity = {
      environment: "staging" as const,
      principal: "staging-gateway",
    };
    const quota = await derivePrincipalQuotaRouteName(ROUTE_KEY, identity);
    const incident = await deriveIncidentRouteName(ROUTE_KEY, {
      ...identity,
      fingerprint: "principal-quota",
    });

    expect(quota).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(quota).not.toContain(identity.environment);
    expect(quota).not.toContain(identity.principal);
    expect(quota).not.toBe(incident);
    await expect(
      derivePrincipalQuotaRouteName(ROUTE_KEY, identity),
    ).resolves.toBe(quota);
  });

  it("uses another opaque domain for deployment registry objects", async () => {
    const registry = await deriveDeploymentRegistryRouteName(ROUTE_KEY, {
      environment: "staging",
      service: "gateway",
    });
    const quota = await derivePrincipalQuotaRouteName(ROUTE_KEY, {
      environment: "staging",
      principal: "gateway",
    });

    expect(registry).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(registry).not.toContain("staging");
    expect(registry).not.toContain("gateway");
    expect(registry).not.toBe(quota);
  });

  it("derives the route only from trusted environment/principal plus event fingerprint", async () => {
    const envelope = createCanonicalEnvelope(
      {
        schema_version: 1,
        event_id: "event-1",
        alert_type: "provider_circuit_open",
        fingerprint: "provider-circuit:openai",
        status: "firing",
        severity: "error",
        title: "Provider circuit opened",
        occurred_at: "2026-07-19T06:00:00Z",
        summary: "The provider returned errors",
        context: { provider: "openai", reason: "upstream_error" },
        evidence_refs: ["config/models.yaml"],
      },
      {
        environment: "staging",
        target_environment: "staging",
        source: "gateway",
        principal: "staging-gateway",
        deployment_id: "gateway-1",
        deployment_sha: "a".repeat(40),
        artifact_digest: `sha256:${"b".repeat(64)}`,
        registry_version: 1,
      },
      { now: Date.parse("2026-07-20T00:00:00Z") },
    );
    await expect(routeNameForEnvelope(ROUTE_KEY, envelope.trusted, envelope.event)).resolves.toBe(
      await deriveIncidentRouteName(ROUTE_KEY, {
        environment: "staging",
        principal: "staging-gateway",
        fingerprint: "provider-circuit:openai",
      }),
    );
  });
});
