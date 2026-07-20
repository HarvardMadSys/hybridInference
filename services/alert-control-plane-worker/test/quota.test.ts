import { describe, expect, it } from "vitest";

import {
  InMemoryPrincipalQuota,
  QuotaLeaseError,
  quotaReservationKey,
  type QuotaReservationIdentity,
} from "../src/quota";

function identity(
  incidentId: string,
  generation = 1,
  environment = "staging",
  principal = "staging-gateway",
): QuotaReservationIdentity {
  return { environment, principal, incidentId, generation };
}

describe("PrincipalQuota", () => {
  it("serializes concurrent retry-shaped reservations under one idempotency key", async () => {
    let now = 1_000;
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 2,
      pendingLeaseMs: 100,
      now: () => now,
    });
    const target = identity("incident-a");

    const results = await Promise.all(
      Array.from({ length: 25 }, () =>
        Promise.resolve().then(() => quota.reserve(target)),
      ),
    );

    expect(results.every((result) => result.admitted)).toBe(true);
    const admitted = results.filter((result) => result.admitted);
    expect(new Set(admitted.map((result) => result.reservation.leaseEpoch))).toEqual(
      new Set([1]),
    );
    expect(admitted.filter((result) => !result.idempotent)).toHaveLength(1);
    expect(quota.activeCount("staging", "staging-gateway")).toBe(1);
    expect(admitted[0]?.reservation.key).toBe(
      quotaReservationKey("incident-a", 1),
    );

    now += 1;
    const confirmed = quota.confirm(target, 1);
    expect(confirmed.state).toBe("confirmed");
    expect(quota.confirm(target, 1)).toBe(confirmed);
  });

  it("enforces the active limit independently per environment and principal", async () => {
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 3,
      pendingLeaseMs: 100,
      now: () => 1_000,
    });

    const results = await Promise.all(
      Array.from({ length: 12 }, (_, index) =>
        Promise.resolve().then(() =>
          quota.reserve(identity(`incident-${index}`)),
        ),
      ),
    );

    expect(results.filter((result) => result.admitted)).toHaveLength(3);
    expect(
      results.filter(
        (result) => !result.admitted && result.reason === "active_limit",
      ),
    ).toHaveLength(9);
    expect(quota.activeCount("staging", "staging-gateway")).toBe(3);
    expect(
      quota.reserve(
        identity(
          "production-incident",
          1,
          "production",
          "production-gateway",
        ),
      ).admitted,
    ).toBe(true);
    expect(
      quota.reserve(
        identity("monitor-incident", 1, "staging", "staging-monitor"),
      ).admitted,
    ).toBe(true);
  });

  it("reclaims only unconfirmed expired leases", () => {
    let now = 10_000;
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 2,
      pendingLeaseMs: 50,
      now: () => now,
    });
    const pending = identity("pending");
    const confirmed = identity("confirmed");
    quota.reserve(pending);
    const confirmation = quota.reserve(confirmed);
    if (!confirmation.admitted) {
      throw new Error("expected confirmation lease");
    }
    quota.confirm(confirmed, confirmation.reservation.leaseEpoch);

    now = 10_051;
    expect(quota.reclaimExpired("staging", "staging-gateway")).toBe(1);
    expect(quota.activeCount("staging", "staging-gateway")).toBe(1);
    expect(
      quota.reserve(identity("replacement")).admitted,
    ).toBe(true);
  });

  it("fences a stale release after an expired reservation is reacquired", () => {
    let now = 1_000;
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 1,
      pendingLeaseMs: 10,
      now: () => now,
    });
    const target = identity("incident-a");
    const first = quota.reserve(target);
    if (!first.admitted) {
      throw new Error("expected first lease");
    }

    now = 1_011;
    const second = quota.reserve(target);
    if (!second.admitted) {
      throw new Error("expected replacement lease");
    }
    expect(second.reservation.leaseEpoch).toBe(2);

    expect(() =>
      quota.release(target, first.reservation.leaseEpoch),
    ).toThrowError(expect.objectContaining<Partial<QuotaLeaseError>>({
      code: "stale_lease",
    }));
    expect(quota.activeCount("staging", "staging-gateway")).toBe(1);
    expect(
      quota.confirm(target, second.reservation.leaseEpoch).state,
    ).toBe("confirmed");
  });

  it("releases an exact generation without releasing the next generation", () => {
    let now = 1_000;
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 2,
      pendingLeaseMs: 100,
      now: () => now,
    });
    const generationOne = identity("incident-a", 1);
    const generationTwo = identity("incident-a", 2);
    const first = quota.reserve(generationOne);
    const second = quota.reserve(generationTwo);
    if (!first.admitted || !second.admitted) {
      throw new Error("expected both generation leases");
    }
    quota.confirm(generationTwo, second.reservation.leaseEpoch);

    now += 1;
    expect(
      quota.release(generationOne, first.reservation.leaseEpoch).state,
    ).toBe("released");
    expect(quota.activeCount("staging", "staging-gateway")).toBe(1);
    expect(
      quota.confirm(generationTwo, second.reservation.leaseEpoch).state,
    ).toBe("confirmed");

    const retriedRelease = quota.release(
      generationOne,
      first.reservation.leaseEpoch,
    );
    expect(retriedRelease.state).toBe("released");
    expect(quota.reserve(generationOne)).toMatchObject({
      admitted: false,
      reason: "reservation_released",
    });
  });

  it("rejects confirmation after the pending lease deadline", () => {
    let now = 1_000;
    const quota = new InMemoryPrincipalQuota({
      activeLimit: 1,
      pendingLeaseMs: 10,
      now: () => now,
    });
    const target = identity("incident-a");
    const lease = quota.reserve(target);
    if (!lease.admitted) {
      throw new Error("expected lease");
    }

    now = 1_010;
    expect(() =>
      quota.confirm(target, lease.reservation.leaseEpoch),
    ).toThrowError(expect.objectContaining<Partial<QuotaLeaseError>>({
      code: "expired_lease",
    }));
    expect(quota.activeCount("staging", "staging-gateway")).toBe(0);
  });
});
