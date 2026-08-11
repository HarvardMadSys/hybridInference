import type { Config } from "./env";

/** A model discovered from the gateway, with the info needed to probe it. */
export interface TargetModel {
  id: string;
  kind: "chat" | "embedding";
}

interface RawModel {
  id?: unknown;
  output_modalities?: unknown;
  on_demand?: unknown;
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
 * Models the catalog marks `on_demand: true` are skipped too — see the comment
 * in the loop; a model dropped this way reads as departed on the next cycle,
 * which resolves any incident that was open on it.
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
  let excluded = 0;
  for (const model of data) {
    // A blank id is not a probe target. Kept, it would be requested as if it were
    // a real model, fail, and still count as a *present* model — enough to make
    // the guard below see a usable catalog while every genuine model reads as
    // departed, which is the exact mass resolution that guard exists to prevent.
    if (typeof model.id !== "string" || model.id.trim() === "" || seen.has(model.id)) {
      continue;
    }
    seen.add(model.id);
    // On-demand models (catalog `on_demand: true`) load lazily on shared GPUs:
    // started by the first request, stopped when idle, refused fast when no
    // GPU is vacant. A synthetic probe is exactly the traffic that defeats
    // that design — every cycle it cold-starts or keeps resident whichever
    // models win the GPU race and reports the rest as outages ("no vacant
    // GPU"), paging on capacity the probe itself is consuming. Their liveness
    // signal is the gateway's own /health probing of the proxy that serves
    // them (routing.yaml local_deployment), not a per-model generation.
    //
    // Strict `=== true`: the flag crosses a JSON boundary, and a string
    // "false" must not silently drop a model from monitoring.
    //
    // Skipped *after* `seen` so a duplicate id cannot re-enter as probeable,
    // and deliberately not counted as a usable target by the guard below — a
    // catalog of only on-demand models leaves the monitor with nothing it may
    // probe, which is a configuration to fail loudly on, not a quiet no-op.
    if (model.on_demand === true) {
      excluded += 1;
      continue;
    }
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
    throw new Error(
      excluded > 0
        ? `empty /models response: no usable probe targets (${excluded} on-demand model(s) excluded)`
        : "empty /models response: no usable probe targets",
    );
  }
  if (excluded > 0) {
    console.log(`discovery: skipped ${excluded} on-demand model(s); probing ${targets.length}.`);
  }
  return targets;
}
