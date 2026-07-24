import { afterEach, describe, expect, it, vi } from "vitest";

import {
  completePendingCanonicalEvent,
  configuredDefaultOwner,
  ControlPlanePreparationError,
  getOrCreatePendingCanonicalEvent,
  modelUnavailableEvent,
  releaseDrainOwner,
  resolveDrainOwner,
  submitPendingCanonicalEvent,
  type StatusMonitorControlPlaneService,
} from "../src/control-plane";
import type { ProbeResult } from "../src/probe";

const VERSION_ID = "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908";

class FakeStmt {
  private args: unknown[] = [];

  constructor(
    private readonly db: FakeD1,
    private readonly sql: string,
  ) {}

  bind(...args: unknown[]): this {
    this.args = args;
    return this;
  }

  async first<T>(): Promise<T | null> {
    if (!/SELECT value FROM meta WHERE key = \?/.test(this.sql)) {
      throw new Error(`unhandled first: ${this.sql}`);
    }
    const value = this.db.meta.get(this.args[0] as string);
    return value === undefined ? null : ({ value } as T);
  }

  async run(): Promise<{ meta: { changes: number } }> {
    if (/INSERT OR REPLACE INTO meta/.test(this.sql)) {
      this.db.meta.set(this.args[0] as string, this.args[1] as string);
      return { meta: { changes: 1 } };
    }
    if (/DELETE FROM meta WHERE key = \?/.test(this.sql)) {
      const changed = this.db.meta.delete(this.args[0] as string);
      return { meta: { changes: changed ? 1 : 0 } };
    }
    throw new Error(`unhandled run: ${this.sql}`);
  }
}

class FakeD1 {
  readonly meta = new Map<string, string>();

  prepare(sql: string): FakeStmt {
    return new FakeStmt(this, sql);
  }
}

function result(overrides: Partial<ProbeResult> = {}): ProbeResult {
  return {
    modelId: "deepseek-v3",
    ok: false,
    checkedAt: "2026-07-24T01:00:00Z",
    latencyMs: null,
    ttftMs: null,
    completionTokens: null,
    throughputTps: null,
    error: "request timed out at 10s; Bearer must-not-be-copied",
    ...overrides,
  };
}

function db(value = new FakeD1()): D1Database {
  return value as unknown as D1Database;
}

describe("model-unavailable source adapter", () => {
  it("produces only the canonical platform-neutral body", () => {
    const event = modelUnavailableEvent(result(), "firing", 2, "event-stable-1");

    expect(event).toEqual({
      schema_version: 1,
      event_id: "event-stable-1",
      alert_type: "model_unavailable",
      fingerprint: "status-monitor:model:deepseek-v3",
      status: "firing",
      severity: "error",
      title: "Model unavailable: deepseek-v3",
      occurred_at: "2026-07-24T01:00:00Z",
      summary: "deepseek-v3 failed 2 consecutive synthetic probes.",
      context: {
        model_id: "deepseek-v3",
        consecutive_failures: 2,
        failure_threshold: 2,
        reason: "timeout",
      },
      evidence_refs: [],
    });
    const serialized = JSON.stringify(event);
    expect(serialized).not.toContain("slack");
    expect(serialized).not.toContain("Bearer");
    expect(serialized).not.toContain("environment");
    expect(serialized).not.toContain("source");
    expect(serialized).not.toContain("deployment");
    expect(serialized).not.toContain("staging.freeinference.org");
  });

  it("uses the same fingerprint and a minimal recovery context", () => {
    const event = modelUnavailableEvent(
      result({ ok: true, error: null, latencyMs: 842 }),
      "resolved",
      2,
      "event-recovery-1",
    );
    expect(event.fingerprint).toBe("status-monitor:model:deepseek-v3");
    expect(event.context).toEqual({ model_id: "deepseek-v3", latency_ms: 842 });
    expect(event.severity).toBe("info");
  });

  it("rejects an invalid failure threshold before persistence", () => {
    expect(() => modelUnavailableEvent(result(), "firing", 0, "event-invalid")).toThrow(
      /positive integer/,
    );
  });
});

describe("drain ownership", () => {
  it("defaults to legacy unless control-plane is selected exactly", () => {
    expect(configuredDefaultOwner(undefined)).toBe("legacy");
    expect(configuredDefaultOwner("legacy")).toBe("legacy");
    expect(configuredDefaultOwner("CONTROL-PLANE")).toBe("legacy");
    expect(configuredDefaultOwner(" control-plane ")).toBe("control-plane");
  });

  it("pins a firing fingerprint and keeps it after the future default changes", async () => {
    const fake = new FakeD1();
    expect(await resolveDrainOwner(db(fake), "status-monitor:model:a", "firing", "legacy")).toBe(
      "legacy",
    );
    expect(
      await resolveDrainOwner(db(fake), "status-monitor:model:a", "resolved", "control-plane"),
    ).toBe("legacy");
  });

  it("treats a pre-migration active recovery as legacy-owned", async () => {
    const fake = new FakeD1();
    expect(
      await resolveDrainOwner(db(fake), "status-monitor:model:old", "resolved", "control-plane"),
    ).toBe("legacy");
    await releaseDrainOwner(db(fake), "status-monitor:model:old");
    expect(
      await resolveDrainOwner(db(fake), "status-monitor:model:new", "firing", "control-plane"),
    ).toBe("control-plane");
  });

  it("fails closed on corrupt ownership state", async () => {
    const fake = new FakeD1();
    fake.meta.set("alert_delivery_owner:v1:status-monitor:model:a", "both");
    await expect(
      resolveDrainOwner(db(fake), "status-monitor:model:a", "firing", "control-plane"),
    ).rejects.toThrow(new ControlPlanePreparationError("drain_ownership_corrupt"));
  });
});

describe("durable pending canonical event", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("reuses the exact event_id and body across attempts", async () => {
    const fake = new FakeD1();
    const firstCandidate = modelUnavailableEvent(
      result(),
      "firing",
      2,
      "event-first",
    );
    const first = await getOrCreatePendingCanonicalEvent(db(fake), firstCandidate);
    const retryCandidate = modelUnavailableEvent(
      result({ checkedAt: "2026-07-24T01:20:00Z", error: "different error" }),
      "firing",
      2,
      "event-must-not-replace-first",
    );
    const retry = await getOrCreatePendingCanonicalEvent(db(fake), retryCandidate);

    expect(retry.eventId).toBe("event-first");
    expect(retry.bodyJson).toBe(first.bodyJson);
    expect(retry.bodyJson).toContain("2026-07-24T01:00:00Z");
    expect(retry.bodyJson).not.toContain("event-must-not-replace-first");
  });

  it("fails closed on corrupt persisted state without logging its contents", async () => {
    const fake = new FakeD1();
    const secretMarker = "do-not-log-this-marker";
    fake.meta.set(
      "alert_delivery_pending:v1:firing:status-monitor:model:deepseek-v3",
      JSON.stringify({ schema_version: 1, body_json: secretMarker }),
    );
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);

    await expect(
      getOrCreatePendingCanonicalEvent(
        db(fake),
        modelUnavailableEvent(result(), "firing", 2, "replacement"),
      ),
    ).rejects.toThrow(new ControlPlanePreparationError("pending_event_corrupt"));
    expect(log).not.toHaveBeenCalled();
  });

  it("submits only to the service binding and never invokes a fallback", async () => {
    const fake = new FakeD1();
    const pending = await getOrCreatePendingCanonicalEvent(
      db(fake),
      modelUnavailableEvent(result(), "firing", 2, "event-submit"),
    );
    const legacyFetch = vi.fn();
    vi.stubGlobal("fetch", legacyFetch);
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const service: StatusMonitorControlPlaneService = {
      submitStatusMonitorEvent: vi.fn(async (_bodyJson, _deploymentId) => {
        throw new Error("ambiguous response");
      }),
    };

    expect(
      await submitPendingCanonicalEvent(
        service,
        pending,
        { id: VERSION_ID } as WorkerVersionMetadata,
      ),
    ).toBe(false);
    expect(legacyFetch).not.toHaveBeenCalled();
    expect(log).toHaveBeenCalledWith("alert control plane service binding submission failed");
    const retry = await getOrCreatePendingCanonicalEvent(
      db(fake),
      modelUnavailableEvent(result(), "firing", 2, "different"),
    );
    expect(retry.bodyJson).toBe(pending.bodyJson);
  });

  it("passes the exact persisted body and immutable Worker version ID", async () => {
    const fake = new FakeD1();
    const pending = await getOrCreatePendingCanonicalEvent(
      db(fake),
      modelUnavailableEvent(result(), "firing", 2, "event-submit-exact"),
    );
    const submitStatusMonitorEvent = vi.fn(async () => ({
      accepted: true as const,
      acknowledgement: {
        accepted: true as const,
        incident_id: "incident-1",
        generation: 1,
        lifecycle_state: "opening",
        action: "opened",
        occurrence_count: 1,
        state_version: 1,
      },
    }));

    await expect(
      submitPendingCanonicalEvent(
        { submitStatusMonitorEvent },
        pending,
        { id: VERSION_ID } as WorkerVersionMetadata,
      ),
    ).resolves.toBe(true);
    expect(submitStatusMonitorEvent).toHaveBeenCalledOnce();
    expect(submitStatusMonitorEvent).toHaveBeenCalledWith(pending.bodyJson, VERSION_ID);
  });

  it.each([
    undefined,
    { id: "" } as WorkerVersionMetadata,
    { id: "not-a-cloudflare-version" } as WorkerVersionMetadata,
  ])("fails closed before RPC for invalid version metadata %#", async (versionMetadata) => {
    const fake = new FakeD1();
    const pending = await getOrCreatePendingCanonicalEvent(
      db(fake),
      modelUnavailableEvent(result(), "firing", 2, "event-invalid-version"),
    );
    const submitStatusMonitorEvent = vi.fn();

    await expect(
      submitPendingCanonicalEvent(
        { submitStatusMonitorEvent },
        pending,
        versionMetadata,
      ),
    ).resolves.toBe(false);
    expect(submitStatusMonitorEvent).not.toHaveBeenCalled();
  });

  it("rejects malformed RPC success without logging or clearing the pending event", async () => {
    const fake = new FakeD1();
    const pending = await getOrCreatePendingCanonicalEvent(
      db(fake),
      modelUnavailableEvent(result(), "firing", 2, "event-malformed-result"),
    );
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const submitStatusMonitorEvent = vi.fn(async () => ({
      accepted: true,
      acknowledgement: { accepted: true, secret: "must-not-be-trusted" },
    })) as unknown as StatusMonitorControlPlaneService["submitStatusMonitorEvent"];

    await expect(
      submitPendingCanonicalEvent(
        { submitStatusMonitorEvent },
        pending,
        { id: VERSION_ID } as WorkerVersionMetadata,
      ),
    ).resolves.toBe(false);
    expect(log).not.toHaveBeenCalled();
    const retry = await getOrCreatePendingCanonicalEvent(
      db(fake),
      modelUnavailableEvent(result(), "firing", 2, "replacement-event"),
    );
    expect(retry.bodyJson).toBe(pending.bodyJson);
  });

  it("clears pending state and releases ownership only after resolved completion", async () => {
    const fake = new FakeD1();
    const fingerprint = "status-monitor:model:deepseek-v3";
    await resolveDrainOwner(db(fake), fingerprint, "firing", "control-plane");
    const firing = await getOrCreatePendingCanonicalEvent(
      db(fake),
      modelUnavailableEvent(result(), "firing", 2, "event-firing"),
    );
    await completePendingCanonicalEvent(db(fake), firing);
    expect(await resolveDrainOwner(db(fake), fingerprint, "resolved", "legacy")).toBe(
      "control-plane",
    );

    const resolved = await getOrCreatePendingCanonicalEvent(
      db(fake),
      modelUnavailableEvent(
        result({ ok: true, error: null, latencyMs: 123 }),
        "resolved",
        2,
        "event-resolved",
      ),
    );
    await completePendingCanonicalEvent(db(fake), resolved);
    expect(await resolveDrainOwner(db(fake), fingerprint, "firing", "legacy")).toBe("legacy");
  });
});
