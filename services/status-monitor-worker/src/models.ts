import type { Config } from "./env";

/** A model discovered from the gateway, with the info needed to probe it. */
export interface TargetModel {
  id: string;
  kind: "chat" | "embedding";
}

interface RawModel {
  id?: unknown;
  output_modalities?: unknown;
}

function kindOf(model: RawModel): "chat" | "embedding" {
  const out = model.output_modalities;
  if (Array.isArray(out) && out.includes("embedding")) {
    return "embedding";
  }
  return "chat";
}

/**
 * Discovers probe targets from `${gateway}/v1/models`.
 *
 * The endpoint is already role-filtered for the prober's API key, so models the
 * account can't access simply don't appear (and won't be reported as outages).
 * `/v1/models` (not `/models`) is used because the edge routes `/v1/*` to the
 * gateway; without Anthropic headers it returns the standard OpenAI list shape.
 *
 * A catalog with no usable target is a discovery failure, not a successful
 * cycle over zero models — see the guard below.
 */
export async function discoverModels(config: Config, apiKey: string): Promise<TargetModel[]> {
  const response = await fetch(`${config.gatewayBaseUrl}/v1/models`, {
    headers: { Authorization: `Bearer ${apiKey}` },
    signal: AbortSignal.timeout(config.probeDeadlineMs),
  });
  if (!response.ok) {
    throw new Error(`models discovery failed: HTTP ${response.status}`);
  }
  const body = (await response.json()) as { data?: unknown };
  // Treat a malformed catalog (non-array `data`) as a discovery failure rather
  // than an empty catalog, so a regression isn't mistaken for "all models
  // removed" and used to wipe history.
  if (!Array.isArray(body.data)) {
    throw new Error("malformed /models response: 'data' is not an array");
  }
  const data = body.data as RawModel[];
  const targets: TargetModel[] = [];
  const seen = new Set<string>();
  for (const model of data) {
    if (typeof model.id !== "string" || seen.has(model.id)) {
      continue;
    }
    seen.add(model.id);
    targets.push({ id: model.id, kind: kindOf(model) });
  }
  // A catalog that yields no probe target is a gateway or authorization failure,
  // not "every model was legitimately removed". Returning empty would report a
  // *successful* cycle over zero models, which deletes all probe history, blanks
  // the dashboard, and — since every previously alerted model is now absent —
  // resolves every open incident as departed. That silent mass recovery hides a
  // total outage and re-arms each failure streak from zero. Failing discovery
  // instead keeps history, keeps incidents open, and pages the cycle alert.
  if (targets.length === 0) {
    throw new Error("empty /models response: no usable probe targets");
  }
  return targets;
}
