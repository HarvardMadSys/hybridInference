import { afterEach, describe, expect, it, vi } from "vitest";

import {
  runServiceBindingGate,
} from "../src/service-binding-gate";
import type {
  StatusMonitorControlPlaneService,
  StatusMonitorRpcResult,
} from "../src/control-plane";

const VERSION_ID = "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908";
const acknowledgement = {
  accepted: true as const,
  incident_id: "incident-1",
  generation: 1,
  lifecycle_state: "opening",
  action: "opened",
  occurrence_count: 1,
  state_version: 1,
};

function env(service: StatusMonitorControlPlaneService) {
  return {
    ALERT_CONTROL_PLANE: service,
    GATE_DEPLOYMENT_ID: VERSION_ID,
    GATE_OCCURRED_AT: "2026-07-24T03:00:00.000Z",
    GATE_RUN_ID: "12345-1",
  };
}

function accepted(): StatusMonitorRpcResult {
  return { accepted: true, acknowledgement };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("runner-local status-monitor Service Binding gate", () => {
  it("submits a deterministic firing and resolution through the named RPC contract", async () => {
    const legacyFetch = vi.fn();
    vi.stubGlobal("fetch", legacyFetch);
    const submitStatusMonitorEvent =
      vi.fn<StatusMonitorControlPlaneService["submitStatusMonitorEvent"]>(
        async () => accepted(),
      );

    const response = await runServiceBindingGate(
      new Request("http://127.0.0.1:8799/run", {
        method: "POST",
        headers: { "x-gate-run-id": "12345-1" },
      }),
      env({ submitStatusMonitorEvent }),
    );

    expect(response.status).toBe(204);
    expect(submitStatusMonitorEvent).toHaveBeenCalledTimes(2);
    const [firingBody, firingVersion] = submitStatusMonitorEvent.mock.calls[0]!;
    const [resolvedBody, resolvedVersion] = submitStatusMonitorEvent.mock.calls[1]!;
    expect(firingVersion).toBe(VERSION_ID);
    expect(resolvedVersion).toBe(VERSION_ID);
    expect(JSON.parse(firingBody)).toMatchObject({
      event_id: "status-monitor-gate:12345-1:firing",
      status: "firing",
      occurred_at: "2026-07-24T03:00:00.000Z",
    });
    expect(JSON.parse(resolvedBody)).toMatchObject({
      event_id: "status-monitor-gate:12345-1:resolved",
      status: "resolved",
      occurred_at: "2026-07-24T03:00:01.000Z",
    });
    expect(legacyFetch).not.toHaveBeenCalled();
  });

  it("conceals requests outside the current gate run and never reaches the binding", async () => {
    const submitStatusMonitorEvent =
      vi.fn<StatusMonitorControlPlaneService["submitStatusMonitorEvent"]>(
        async () => accepted(),
      );
    const response = await runServiceBindingGate(
      new Request("http://127.0.0.1:8799/run", { method: "POST" }),
      env({ submitStatusMonitorEvent }),
    );

    expect(response.status).toBe(404);
    expect(submitStatusMonitorEvent).not.toHaveBeenCalled();
  });

  it("fails closed but still sends the resolution after a rejected firing", async () => {
    const submitStatusMonitorEvent =
      vi.fn<StatusMonitorControlPlaneService["submitStatusMonitorEvent"]>(
        async () => ({
          accepted: false,
          errorCode: "unknown_deployment",
        }),
      );
    const response = await runServiceBindingGate(
      new Request("http://127.0.0.1:8799/run", {
        method: "POST",
        headers: { "x-gate-run-id": "12345-1" },
      }),
      env({ submitStatusMonitorEvent }),
    );

    expect(response.status).toBe(502);
    expect(submitStatusMonitorEvent).toHaveBeenCalledTimes(2);
    expect(JSON.parse(submitStatusMonitorEvent.mock.calls[1]![0])).toMatchObject({
      status: "resolved",
    });
    await expect(response.json()).resolves.toEqual({
      error: "synthetic_rpc_rejected",
    });
  });

  it("retries an ambiguous RPC with byte-identical event bodies", async () => {
    const submitStatusMonitorEvent =
      vi.fn<StatusMonitorControlPlaneService["submitStatusMonitorEvent"]>()
        .mockRejectedValueOnce(new Error("ambiguous response"))
        .mockResolvedValue(accepted());

    const response = await runServiceBindingGate(
      new Request("http://127.0.0.1:8799/run", {
        method: "POST",
        headers: { "x-gate-run-id": "12345-1" },
      }),
      env({ submitStatusMonitorEvent }),
    );

    expect(response.status).toBe(204);
    expect(submitStatusMonitorEvent).toHaveBeenCalledTimes(3);
    expect(submitStatusMonitorEvent.mock.calls[0]![0]).toBe(
      submitStatusMonitorEvent.mock.calls[1]![0],
    );
  });
});
