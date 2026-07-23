import { describe, expect, it, vi } from "vitest";

import type { ActionClaim } from "../src/outbox";
import {
  handlePrincipalQuotaRequest,
  QuotaActionExecutor,
} from "../src/quota-runtime";
import { InMemoryPrincipalQuota } from "../src/quota";
import { pendingAction } from "./fakes";

const ROUTE_KEY = "quota-runtime-test-route-key-material-32-bytes-minimum";

function request(
  operation: "reserve" | "confirm" | "release",
  body: Record<string, unknown>,
): Request {
  return new Request(`https://quota.internal/internal/${operation}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function payload(
  incidentId: string,
  generation = 1,
): Record<string, unknown> {
  return {
    environment: "staging",
    principal: "staging-gateway",
    incident_id: incidentId,
    generation,
  };
}

function namespaceFor(
  quota: InMemoryPrincipalQuota,
  requests: string[] = [],
): DurableObjectNamespace {
  const stub = {
    fetch: vi.fn(async (input: RequestInfo | URL) => {
      const incoming =
        input instanceof Request ? input : new Request(input.toString());
      requests.push(new URL(incoming.url).pathname);
      return handlePrincipalQuotaRequest(
        quota,
        incoming as unknown as Request,
      );
    }),
  };
  return {
    idFromName: vi.fn().mockReturnValue({ toString: () => "quota-id" }),
    get: vi.fn().mockReturnValue(stub),
  } as unknown as DurableObjectNamespace;
}

function claim(
  type: "reserve_quota" | "release_quota",
  mode: ActionClaim["mode"],
  actionPayload: Record<string, unknown>,
): ActionClaim {
  return {
    mode,
    action: pendingAction({
      type,
      payload: actionPayload,
      generation: Number(actionPayload.generation),
    }),
  };
}

describe("principal quota internal contract", () => {
  it("reserves, confirms, and releases one fenced generation", async () => {
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 1,
      pendingLeaseMs: 60_000,
      now: () => 1_000,
    });
    const identity = payload("incident-a");

    const reserved = await handlePrincipalQuotaRequest(
      quota,
      request("reserve", identity),
    );
    await expect(reserved.json()).resolves.toEqual({
      admitted: true,
      lease_epoch: 1,
    });

    const confirmed = await handlePrincipalQuotaRequest(
      quota,
      request("confirm", { ...identity, lease_epoch: 1 }),
    );
    await expect(confirmed.json()).resolves.toEqual({
      confirmed: true,
      lease_epoch: 1,
    });

    const denied = await handlePrincipalQuotaRequest(
      quota,
      request("reserve", payload("incident-b")),
    );
    await expect(denied.json()).resolves.toEqual({ admitted: false });

    const released = await handlePrincipalQuotaRequest(
      quota,
      request("release", { ...identity, lease_epoch: 1 }),
    );
    await expect(released.json()).resolves.toEqual({ released: true });
    expect(quota.activeCount("staging", "staging-gateway")).toBe(0);
  });

  it("does not hide an absent release that could indicate route-key drift", async () => {
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 1,
      pendingLeaseMs: 60_000,
      now: () => 1_000,
    });
    const response = await handlePrincipalQuotaRequest(
      quota,
      request("release", { ...payload("absent"), lease_epoch: 1 }),
    );
    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toEqual({
      error: "unknown_reservation",
    });
  });

  it("rejects extra fields and does not reflect their values", async () => {
    const secret = "xoxb-must-not-be-reflected";
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 1,
      pendingLeaseMs: 60_000,
    });
    const response = await handlePrincipalQuotaRequest(
      quota,
      request("reserve", { ...payload("incident-a"), token: secret }),
    );
    expect(response.status).toBe(400);
    const body = await response.text();
    expect(body).toBe('{"error":"invalid_reservation"}');
    expect(body).not.toContain(secret);
  });
});

describe("quota outbox executor", () => {
  it("uses execute to reserve and reconcile to confirm before success", async () => {
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 1,
      pendingLeaseMs: 60_000,
      now: () => 1_000,
    });
    const requests: string[] = [];
    const namespace = namespaceFor(quota, requests);
    const executor = new QuotaActionExecutor(namespace, ROUTE_KEY, () => 1_001);
    const reservePayload = payload("incident-a");

    await expect(
      executor.execute(claim("reserve_quota", "execute", reservePayload)),
    ).resolves.toEqual({
      outcome: "uncertain",
      error: "quota_confirmation_pending",
      reconcileAtMs: 1_001,
    });
    expect(quota.activeCount("staging", "staging-gateway")).toBe(1);

    await expect(
      executor.execute(claim("reserve_quota", "reconcile", reservePayload)),
    ).resolves.toEqual({
      outcome: "success",
      result: { admitted: true, lease_epoch: 1 },
    });

    await expect(
      executor.execute(
        claim("release_quota", "execute", {
          ...reservePayload,
          lease_epoch: 1,
        }),
      ),
    ).resolves.toEqual({ outcome: "success", result: {} });
    expect(quota.activeCount("staging", "staging-gateway")).toBe(0);
    expect(requests).toEqual([
      "/internal/reserve",
      "/internal/reserve",
      "/internal/confirm",
      "/internal/release",
    ]);
    expect(namespace.idFromName).toHaveBeenCalledTimes(4);
    const names = (namespace.idFromName as ReturnType<typeof vi.fn>).mock.calls.map(
      ([name]) => name,
    );
    expect(new Set(names)).toHaveLength(1);
    expect(names[0]).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(names[0]).not.toContain("staging-gateway");
  });

  it("returns only a stable uncertain code when the binding call throws", async () => {
    const binding = {
      idFromName: vi.fn().mockReturnValue({}),
      get: vi.fn().mockReturnValue({
        fetch: vi.fn().mockRejectedValue(
          new Error("Bearer xoxb-secret response body"),
        ),
      }),
    } as unknown as DurableObjectNamespace;
    const executor = new QuotaActionExecutor(binding, ROUTE_KEY);

    const result = await executor.execute(
      claim("reserve_quota", "execute", payload("incident-a")),
    );
    expect(result).toEqual({
      outcome: "uncertain",
      error: "quota_request_uncertain",
    });
    expect(JSON.stringify(result)).not.toContain("xoxb-secret");
  });

  it("requires manual reconciliation when a release route has no reservation", async () => {
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 1,
      pendingLeaseMs: 60_000,
    });
    const executor = new QuotaActionExecutor(namespaceFor(quota), ROUTE_KEY);

    await expect(
      executor.execute(
        claim("release_quota", "execute", {
          ...payload("missing"),
          lease_epoch: 1,
        }),
      ),
    ).resolves.toEqual({
      outcome: "manual_reconciliation_required",
      error: "quota_reservation_missing",
    });
  });
});
