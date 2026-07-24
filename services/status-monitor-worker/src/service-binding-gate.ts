import {
  isStatusMonitorRpcResult,
  modelUnavailableEvent,
  type ModelUnavailableAlertEvent,
  type StatusMonitorControlPlaneService,
} from "./control-plane";
import type { ProbeResult } from "./probe";

interface GateEnv {
  readonly ALERT_CONTROL_PLANE: StatusMonitorControlPlaneService;
  readonly GATE_DEPLOYMENT_ID: string;
  readonly GATE_OCCURRED_AT: string;
  readonly GATE_RUN_ID: string;
}

const CLOUDFLARE_VERSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RUN_ID_RE = /^[1-9][0-9]{0,19}-[1-9][0-9]{0,9}$/;

function jsonResponse(value: unknown, status: number): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "application/json; charset=utf-8",
    },
  });
}

function validEnvironment(env: GateEnv): boolean {
  return (
    CLOUDFLARE_VERSION_ID_RE.test(env.GATE_DEPLOYMENT_ID) &&
    RUN_ID_RE.test(env.GATE_RUN_ID) &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(env.GATE_OCCURRED_AT) &&
    !Number.isNaN(Date.parse(env.GATE_OCCURRED_AT))
  );
}

function event(
  env: GateEnv,
  status: "firing" | "resolved",
): ModelUnavailableAlertEvent {
  const modelId = `synthetic-control-plane-gate-${env.GATE_RUN_ID}`;
  const firing = status === "firing";
  const occurredAt = new Date(
    Date.parse(env.GATE_OCCURRED_AT) + (firing ? 0 : 1_000),
  ).toISOString();
  const probe: ProbeResult = {
    modelId,
    ok: !firing,
    checkedAt: occurredAt,
    latencyMs: firing ? null : 0,
    ttftMs: null,
    completionTokens: null,
    throughputTps: null,
    error: firing ? "synthetic gate failure" : null,
  };
  return modelUnavailableEvent(
    probe,
    status,
    1,
    `status-monitor-gate:${env.GATE_RUN_ID}:${status}`,
  );
}

async function submit(
  env: GateEnv,
  alertEvent: ModelUnavailableAlertEvent,
): Promise<boolean> {
  const bodyJson = JSON.stringify(alertEvent);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const result: unknown = await env.ALERT_CONTROL_PLANE.submitStatusMonitorEvent(
        bodyJson,
        env.GATE_DEPLOYMENT_ID,
      );
      if (isStatusMonitorRpcResult(result)) {
        if (result.accepted) return true;
        if (result.errorCode !== "control_plane_unavailable") return false;
      }
    } catch {
      // Retry only the exact same event bytes; never log RPC arguments.
    }
  }
  return false;
}

export async function runServiceBindingGate(
  request: Request,
  env: GateEnv,
): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/healthz") {
    return new Response(null, { status: 204 });
  }
  if (
    request.method !== "POST" ||
    url.pathname !== "/run" ||
    !validEnvironment(env) ||
    request.headers.get("x-gate-run-id") !== env.GATE_RUN_ID
  ) {
    return jsonResponse({ error: "not_found" }, 404);
  }

  try {
    const firingAccepted = await submit(env, event(env, "firing"));
    // Always attempt the resolution: a lost firing response may still have
    // opened the synthetic incident, and replaying fixed event bytes is safe.
    const resolvedAccepted = await submit(env, event(env, "resolved"));
    if (!firingAccepted || !resolvedAccepted) {
      return jsonResponse({ error: "synthetic_rpc_rejected" }, 502);
    }
    return new Response(null, { status: 204 });
  } catch {
    console.error("status-monitor service binding gate failed");
    return jsonResponse({ error: "synthetic_rpc_unavailable" }, 502);
  }
}

export default {
  fetch: runServiceBindingGate,
} satisfies ExportedHandler<GateEnv>;
