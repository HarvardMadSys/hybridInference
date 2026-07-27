import {
  type CycleStatus,
  modelsFailingStreak,
  prepareAlertStateWrite,
  readAlertState,
  readCycleAlertState,
  writeCycleAlertState,
} from "./db";
import {
  ControlPlanePreparationError,
  configuredDefaultOwner,
  getOrCreatePendingCanonicalEvent,
  hasControlPlaneDrainOwner,
  listPendingCanonicalEvents,
  modelUnavailableDepartureEvent,
  modelUnavailableEvent,
  prepareDrainOwnerRelease,
  preparePendingCanonicalEventCompletion,
  readDrainOwner,
  resolveDrainOwner,
  submitPendingCanonicalEvent,
  type AlertDeliveryOwner,
  type ModelUnavailableAlertEvent,
  type ModelUnavailableStatus,
} from "./control-plane";
import type { Config, Env } from "./env";
import type { ProbeResult } from "./probe";
import {
  codexRelayConfig,
  type CodexAlertEvent,
  createCodexAlertEvent,
  hasAlertDestination,
  modelAlertFingerprint,
  type NewCodexAlertEvent,
  postCodexAlert,
  stormAlertFingerprint,
} from "./oncall";

/** Local/dev gateway hosts that never indicate a real deployment. */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"]);

/**
 * Best-effort deployment environment for the gateway the probes run against,
 * derived from its host (mirrors the backend's alert environment detection).
 */
export function deriveEnvironment(gatewayBaseUrl: string): string {
  let hostname: string;
  try {
    // `.hostname` (not `.host`) excludes the port and keeps IPv6 brackets intact,
    // so `freeinference.org:8443` still matches and `[::1]` isn't truncated.
    hostname = new URL(gatewayBaseUrl).hostname.toLowerCase();
  } catch {
    return "unknown";
  }
  if (!hostname || LOCAL_HOSTS.has(hostname)) return "local";
  if (hostname.includes("staging")) return "staging";
  if (hostname.endsWith("freeinference.org")) return "production";
  return "unknown";
}

/**
 * Escape Slack mrkdwn control characters in untrusted text.
 *
 * Slack interprets `<...>` sequences specially (`<!channel>`, `<@U…>`), so any
 * upstream-controlled value (e.g. a provider error message) interpolated into an
 * alert must escape `&`, `<`, and `>` so it renders literally. `&` is escaped
 * first to avoid double-encoding the others.
 */
export function escapeSlackText(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function header(emoji: string, title: string, config: Config, checkedAt: string): string[] {
  return [`${emoji} *${title}*`, `_${checkedAt} · ${deriveEnvironment(config.gatewayBaseUrl)}_`, ""];
}

/** Slack message for a model that has failed `threshold` consecutive probes. */
export function formatModelDownAlert(config: Config, result: ProbeResult, threshold: number): string {
  const modelId = result.modelId.replace(/`/g, "");
  const error = result.error ? escapeSlackText(result.error) : "(no error message)";
  return [
    ...header("\u{1F6A8}", `Model down: \`${modelId}\``, config, result.checkedAt),
    `• *Failed the last ${threshold} probes in a row.*`,
    `• *Last error:* ${error}`,
    `• *Gateway:* ${config.gatewayBaseUrl}`,
  ].join("\n");
}

/** Slack message for a model whose probe succeeded after a down alert. */
export function formatModelRecoveredAlert(config: Config, result: ProbeResult): string {
  const modelId = result.modelId.replace(/`/g, "");
  return [
    ...header("✅", `Model recovered: \`${modelId}\``, config, result.checkedAt),
    `• *Gateway:* ${config.gatewayBaseUrl}`,
  ].join("\n");
}

// Cap on model ids spelled out in a storm summary; the rest are counted as "+N more".
const SUMMARY_LIST_LIMIT = 25;

function summaryList(results: ProbeResult[]): string {
  const shown = results.slice(0, SUMMARY_LIST_LIMIT).map((r) => `\`${r.modelId.replace(/`/g, "")}\``);
  const extra = results.length - shown.length;
  return extra > 0 ? `${shown.join(", ")} (+${extra} more)` : shown.join(", ");
}

/** One Slack message for a batch of models that went down in the same cycle. */
export function formatModelsDownSummary(
  config: Config,
  results: ProbeResult[],
  threshold: number,
): string {
  return [
    ...header("\u{1F6A8}", `${results.length} models down`, config, results[0]?.checkedAt ?? ""),
    `• *Failed the last ${threshold} probes in a row:* ${summaryList(results)}`,
    `• *Gateway:* ${config.gatewayBaseUrl}`,
  ].join("\n");
}

/** One Slack message for a batch of models that recovered in the same cycle. */
export function formatModelsRecoveredSummary(config: Config, results: ProbeResult[]): string {
  return [
    ...header("✅", `${results.length} models recovered`, config, results[0]?.checkedAt ?? ""),
    `• ${summaryList(results)}`,
    `• *Gateway:* ${config.gatewayBaseUrl}`,
  ].join("\n");
}

/** Slack message when the whole probe cycle fails (gateway down / key rejected). */
export function formatCycleDownAlert(config: Config, status: CycleStatus): string {
  const error = status.error ? escapeSlackText(status.error) : "(no error message)";
  return [
    ...header("\u{1F6A8}", "Monitoring cycle failing", config, status.checkedAt ?? ""),
    `• *Error:* ${error}`,
    `• *Impact:* no models could be probed this cycle.`,
    `• *Gateway:* ${config.gatewayBaseUrl}`,
  ].join("\n");
}

/** Slack message when the probe cycle succeeds again after a cycle-down alert. */
export function formatCycleRecoveredAlert(config: Config, status: CycleStatus): string {
  return [
    ...header("✅", "Monitoring cycle recovered", config, status.checkedAt ?? ""),
    `• *Gateway:* ${config.gatewayBaseUrl}`,
  ].join("\n");
}

/** POST `{"text": message}` to a Slack incoming webhook. Returns true on 2xx. */
export async function postSlack(webhookUrl: string, message: string): Promise<boolean> {
  try {
    const resp = await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: message }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!resp.ok) {
      console.error(`slack webhook returned HTTP ${resp.status}`);
    }
    return resp.ok;
  } catch {
    // The webhook URL is itself the credential, and a Workers fetch failure can
    // embed the request URL in its message — so never log the caught error
    // (mirrors postCodexAlert, which redacts for the same reason).
    console.error("slack webhook post failed");
    return false;
  }
}

function occurredAt(value: string | null | undefined): string {
  return value && !Number.isNaN(Date.parse(value)) ? value : new Date().toISOString();
}

function probeContext(result: ProbeResult): Record<string, unknown> {
  return {
    model_id: result.modelId,
    ok: result.ok,
    checked_at: result.checkedAt,
    latency_ms: result.latencyMs,
    ttft_ms: result.ttftMs,
    completion_tokens: result.completionTokens,
    throughput_tps: result.throughputTps,
    error: result.error?.slice(0, 4_000) ?? null,
  };
}

function workerAlertEvent(
  config: Config,
  event: Omit<NewCodexAlertEvent, "environment">,
): CodexAlertEvent {
  return createCodexAlertEvent({
    ...event,
    environment: deriveEnvironment(config.gatewayBaseUrl),
  });
}

function modelDownEvent(config: Config, result: ProbeResult, threshold: number): CodexAlertEvent {
  return workerAlertEvent(config, {
    fingerprint: modelAlertFingerprint(result.modelId),
    status: "firing",
    severity: "error",
    title: `Model down: ${result.modelId}`,
    occurred_at: occurredAt(result.checkedAt),
    summary: `${result.modelId} failed ${threshold} consecutive probes: ${result.error ?? "no error message"}`,
    context: {
      alert_type: "model",
      gateway_base_url: config.gatewayBaseUrl,
      failure_threshold: threshold,
      probe: probeContext(result),
    },
    slack_text: formatModelDownAlert(config, result, threshold),
  });
}

function modelRecoveredEvent(
  config: Config,
  result: ProbeResult,
  fingerprint = modelAlertFingerprint(result.modelId),
): CodexAlertEvent {
  return workerAlertEvent(config, {
    fingerprint,
    status: "resolved",
    severity: "info",
    title: `Model recovered: ${result.modelId}`,
    occurred_at: occurredAt(result.checkedAt),
    summary: `${result.modelId} recovered after a down alert.`,
    context: {
      alert_type: "model",
      gateway_base_url: config.gatewayBaseUrl,
      probe: probeContext(result),
    },
    slack_text: formatModelRecoveredAlert(config, result),
  });
}

function sortedProbeContext(results: ProbeResult[]): Record<string, unknown>[] {
  return [...results]
    .sort((a, b) => (a.modelId < b.modelId ? -1 : a.modelId > b.modelId ? 1 : 0))
    .map(probeContext);
}

function modelsDownEvent(
  config: Config,
  results: ProbeResult[],
  threshold: number,
): CodexAlertEvent {
  return workerAlertEvent(config, {
    fingerprint: stormAlertFingerprint(results.map((result) => result.modelId)),
    status: "firing",
    severity: "critical",
    title: `${results.length} models down`,
    occurred_at: occurredAt(results[0]?.checkedAt),
    summary: `${results.length} models failed ${threshold} consecutive probes.`,
    context: {
      alert_type: "model_storm",
      gateway_base_url: config.gatewayBaseUrl,
      failure_threshold: threshold,
      models: sortedProbeContext(results),
    },
    slack_text: formatModelsDownSummary(config, results, threshold),
  });
}

function modelsRecoveredEvent(
  config: Config,
  results: ProbeResult[],
  fingerprint = stormAlertFingerprint(results.map((result) => result.modelId)),
): CodexAlertEvent {
  return workerAlertEvent(config, {
    fingerprint,
    status: "resolved",
    severity: "info",
    title: `${results.length} models recovered`,
    occurred_at: occurredAt(results[0]?.checkedAt),
    summary: `${results.length} models recovered after down alerts.`,
    context: {
      alert_type: "model_storm",
      gateway_base_url: config.gatewayBaseUrl,
      models: sortedProbeContext(results),
    },
    slack_text: formatModelsRecoveredSummary(config, results),
  });
}

function cycleEvent(config: Config, status: CycleStatus): CodexAlertEvent {
  const firing = !status.ok;
  return workerAlertEvent(config, {
    fingerprint: "status-monitor:cycle",
    status: firing ? "firing" : "resolved",
    severity: firing ? "critical" : "info",
    title: firing ? "Monitoring cycle failing" : "Monitoring cycle recovered",
    occurred_at: occurredAt(status.checkedAt),
    summary: firing
      ? `The monitoring cycle failed: ${status.error ?? "no error message"}`
      : "The monitoring cycle recovered.",
    context: {
      alert_type: "cycle",
      gateway_base_url: config.gatewayBaseUrl,
      cycle: {
        ok: status.ok,
        checked_at: status.checkedAt,
        error: status.error,
      },
    },
    slack_text: firing
      ? formatCycleDownAlert(config, status)
      : formatCycleRecoveredAlert(config, status),
  });
}

async function deliverAlert(env: Env, event: CodexAlertEvent): Promise<boolean> {
  const relay = codexRelayConfig(env);
  if (relay && (await postCodexAlert(relay, event))) return true;

  const webhookUrl = env.SLACK_WEBHOOK_URL?.trim();
  if (!relay && !webhookUrl) {
    // Storm, cycle, and legacy-drain alerts have no Control Plane path yet, so a
    // deployment without either legacy destination drops them with no post ever
    // attempted. The undelivered edge re-fires next cycle, making this line the
    // only signal that the most severe alert class is going nowhere. The title
    // is bounded and public (model ids); slack_text is deliberately excluded.
    console.error(`legacy alert undeliverable (no relay or webhook configured): ${event.title}`);
    return false;
  }
  return webhookUrl ? postSlack(webhookUrl, event.slack_text) : false;
}

interface ModelDeliveryResult {
  delivered: boolean;
  completionStatements: D1PreparedStatement[];
}

async function deliverModelAlert(
  env: Env,
  result: ProbeResult,
  status: ModelUnavailableStatus,
  threshold: number,
  defaultOwner: AlertDeliveryOwner,
  legacyEvent: CodexAlertEvent,
  canonicalEvent?: ModelUnavailableAlertEvent,
): Promise<ModelDeliveryResult> {
  const fingerprint = modelAlertFingerprint(result.modelId);
  const owner = await resolveDrainOwner(env.DB, fingerprint, status, defaultOwner);
  if (owner === "legacy") {
    const delivered = await deliverAlert(env, legacyEvent);
    return {
      delivered,
      completionStatements:
        delivered && status === "resolved"
          ? [prepareDrainOwnerRelease(env.DB, fingerprint)]
          : [],
    };
  }

  const pending = await getOrCreatePendingCanonicalEvent(
    env.DB,
    canonicalEvent ?? modelUnavailableEvent(result, status, threshold),
  );
  const delivered = await submitPendingCanonicalEvent(
    env.ALERT_CONTROL_PLANE,
    pending,
    env.CF_VERSION_METADATA,
  );
  return {
    delivered,
    completionStatements: delivered
      ? preparePendingCanonicalEventCompletion(env.DB, pending)
      : [],
  };
}

function incidentFingerprint(modelId: string, stateValue: string): string {
  return stateValue.startsWith("status-monitor:")
    ? stateValue
    : modelAlertFingerprint(modelId);
}

/** Which models to page this cycle, plus the carried-over alert state. */
export interface AlertDecision {
  /** Models that newly crossed the failure threshold and should page as down. */
  down: ProbeResult[];
  /** Models that came back up after a down alert and should page as recovered. */
  recovered: ProbeResult[];
  /**
   * `prevState` minus any model no longer probed this cycle (so state can't grow
   * without bound). The down/recovery transitions are intentionally NOT applied
   * here — the caller commits them only after a confirmed delivery.
   */
  baseState: Record<string, string>;
}

/**
 * Edge-triggered alert decision.
 *
 * `failing` holds the models whose most recent `threshold` probes were all
 * failures. `prevState` maps a model to the incident fingerprint used when it
 * was alerted as down; its presence means we've already paged for the current
 * outage. Legacy timestamp values are treated as individual model incidents.
 * A model is paged *down* only on the transition into the failing set (so a
 * sustained outage pages once, not every 20-minute cron), and *recovered* only
 * when a probe succeeds after a down alert.
 *
 * This function is pure: it decides *what* to send but does not record that it
 * was sent. {@link runAlerts} applies the state transition only for an alert
 * whose POST actually succeeded, so a destination outage retries next cycle
 * instead of silently dropping the page.
 */
export function decideAlerts(
  results: ProbeResult[],
  failing: Set<string>,
  prevState: Record<string, string>,
): AlertDecision {
  // Null prototype so model ids like "constructor" don't read as already
  // alerted (inherited property) and "__proto__" persists as a real key.
  const baseState: Record<string, string> = Object.assign(Object.create(null), prevState);
  const present = new Set(results.map((r) => r.modelId));
  for (const id of Object.keys(baseState)) {
    if (!present.has(id)) delete baseState[id];
  }

  const down: ProbeResult[] = [];
  const recovered: ProbeResult[] = [];
  for (const r of results) {
    const alerted = Object.hasOwn(baseState, r.modelId);
    if (failing.has(r.modelId)) {
      if (!alerted) down.push(r);
    } else if (r.ok && alerted) {
      recovered.push(r);
    }
  }
  return { down, recovered, baseState };
}

/**
 * Evaluates probe results and sends alerts for models that failed
 * `config.alertFailureThreshold` consecutive probes (and recovery notices for
 * those that come back). Codex on-call is attempted first when configured, with
 * the Slack webhook as a fallback. A legacy-only configuration is a no-op without
 * either destination; Control Plane transitions remain durable while its binding
 * is unavailable.
 *
 * Runs inside the probe cycle while it holds the cycle lock, so the
 * read-modify-write of the alert state is never raced by an overlapping cron.
 * A model is only recorded as alerted once its page is confirmed delivered, and
 * its state is only cleared once its recovery notice is delivered — so a
 * destination outage causes a retry on the next cycle rather than a lost alert.
 */
export async function runAlerts(env: Env, config: Config, results: ProbeResult[]): Promise<void> {
  const threshold = config.alertFailureThreshold;
  const defaultOwner = configuredDefaultOwner(env.ALERT_DEFAULT_OWNER);
  const hasLegacyDestination = hasAlertDestination(env);
  const pendingEvents = await listPendingCanonicalEvents(env.DB);
  if (
    !hasLegacyDestination &&
    env.ALERT_CONTROL_PLANE === undefined &&
    defaultOwner === "legacy" &&
    pendingEvents.length === 0 &&
    !(await hasControlPlaneDrainOwner(env.DB))
  ) {
    return;
  }
  // Only a model that failed *this* cycle can newly cross the threshold; limiting
  // the streak lookup to those keeps D1 rows_read at threshold × (failed models).
  const failedNow = results.filter((r) => !r.ok).map((r) => r.modelId);
  const failing = await modelsFailingStreak(env.DB, failedNow, threshold);
  const prevState = await readAlertState(env.DB);
  const { down: candidateDown, recovered: candidateRecovered, baseState } = decideAlerts(
    results,
    failing,
    prevState,
  );

  // "Absent from this cycle" only means "gone" when this cycle actually observed
  // a catalog. An empty result set is evidence about nothing, so neither the
  // state pruning in `decideAlerts` nor the departure inference below may act on
  // it: pruning would drop each model's fingerprint mapping — stranding its open
  // incident, because a later healthy probe no longer counts as a recovery — and
  // departure would resolve every incident at once during what is almost
  // certainly a total outage. `discoverModels` already fails the cycle before the
  // alerter runs; this keeps both inferences sound if runAlerts is ever reached
  // another way.
  const observedCatalog = results.length > 0;
  const nextState: Record<string, string> = Object.assign(
    Object.create(null),
    observedCatalog ? baseState : prevState,
  );
  const completionStatements: D1PreparedStatement[] = [];
  const pendingModelIds = new Set<string>();
  for (const pending of pendingEvents) {
    const expectedStatus = Object.hasOwn(prevState, pending.modelId) ? "resolved" : "firing";
    if (pendingModelIds.has(pending.modelId) || pending.status !== expectedStatus) {
      throw new ControlPlanePreparationError("pending_state_conflict");
    }
    pendingModelIds.add(pending.modelId);
    if (pending.status === "resolved") {
      nextState[pending.modelId] = prevState[pending.modelId];
    }
  }

  const replayed = await Promise.all(
    pendingEvents.map(async (pending) => ({
      pending,
      delivered: await submitPendingCanonicalEvent(
        env.ALERT_CONTROL_PLANE,
        pending,
        env.CF_VERSION_METADATA,
      ),
    })),
  );
  for (const { pending, delivered } of replayed) {
    if (!delivered) continue;
    if (pending.status === "firing") {
      nextState[pending.modelId] = pending.fingerprint;
    } else {
      delete nextState[pending.modelId];
    }
    completionStatements.push(...preparePendingCanonicalEventCompletion(env.DB, pending));
  }

  const departedControlPlaneModels = new Map<string, string>();
  if (observedCatalog) {
    const presentModelIds = new Set(results.map((result) => result.modelId));
    for (const [modelId, stateValue] of Object.entries(prevState)) {
      if (presentModelIds.has(modelId) || pendingModelIds.has(modelId)) continue;
      const fingerprint = incidentFingerprint(modelId, stateValue);
      const owner = await readDrainOwner(env.DB, fingerprint);
      if (owner === "control-plane") {
        departedControlPlaneModels.set(modelId, fingerprint);
      } else if (owner === "legacy") {
        completionStatements.push(prepareDrainOwnerRelease(env.DB, fingerprint));
      }
    }
  }

  const departedResults = await Promise.all(
    [...departedControlPlaneModels].map(async ([modelId, fingerprint]) => {
      const result: ProbeResult = {
        modelId,
        ok: true,
        checkedAt: results[0]?.checkedAt ?? new Date().toISOString(),
        latencyMs: null,
        ttftMs: null,
        completionTokens: null,
        throughputTps: null,
        error: null,
      };
      const delivery = await deliverModelAlert(
        env,
        result,
        "resolved",
        threshold,
        defaultOwner,
        modelRecoveredEvent(config, result, fingerprint),
        modelUnavailableDepartureEvent(modelId, result.checkedAt),
      );
      return { modelId, fingerprint, ...delivery };
    }),
  );
  for (const departed of departedResults) {
    if (departed.delivered) {
      delete nextState[departed.modelId];
      completionStatements.push(...departed.completionStatements);
    } else {
      nextState[departed.modelId] = departed.fingerprint;
    }
  }

  // A pending transition owns this cycle even if the latest probe reversed. It
  // must converge before an opposite edge or a legacy storm summary can run.
  const canOpenNewIndividualIncident =
    defaultOwner === "control-plane" || hasLegacyDestination;
  const down = candidateDown.filter(
    (result) =>
      !pendingModelIds.has(result.modelId) && canOpenNewIndividualIncident,
  );
  const recovered = candidateRecovered.filter(
    (result) => !pendingModelIds.has(result.modelId),
  );
  const storm = config.alertStormThreshold;

  // A provider-wide blip can take down many models at once. Past `storm`, collapse
  // them into one summary message so the channel isn't flooded with one page per
  // model; below it, page individually (concurrently, so a batch doesn't hold the
  // cycle lock for count × per-request timeout). Either way a model's state
  // transition is committed only once its page is confirmed delivered, so a failed
  // POST retries next cycle instead of dropping the alert.
  if (down.length > storm) {
    const event = modelsDownEvent(config, down, threshold);
    if (await deliverAlert(env, event)) {
      for (const r of down) nextState[r.modelId] = event.fingerprint;
    }
  } else {
    const sent = await Promise.all(
      down.map(async (r) => {
        const event = modelDownEvent(config, r, threshold);
        const delivery = await deliverModelAlert(
          env,
          r,
          "firing",
          threshold,
          defaultOwner,
          event,
        );
        return {
          modelId: r.modelId,
          fingerprint: event.fingerprint,
          ...delivery,
        };
      }),
    );
    for (const r of sent) {
      if (!r.delivered) continue;
      nextState[r.modelId] = r.fingerprint;
      completionStatements.push(...r.completionStatements);
    }
  }

  const activeModelsByFingerprint = new Map<string, string[]>();
  for (const [modelId, stateValue] of Object.entries(baseState)) {
    const fingerprint = incidentFingerprint(modelId, stateValue);
    const activeModels = activeModelsByFingerprint.get(fingerprint) ?? [];
    activeModels.push(modelId);
    activeModelsByFingerprint.set(fingerprint, activeModels);
  }

  const recoveredByFingerprint = new Map<string, ProbeResult[]>();
  for (const result of recovered) {
    const fingerprint = incidentFingerprint(result.modelId, baseState[result.modelId]);
    const grouped = recoveredByFingerprint.get(fingerprint) ?? [];
    grouped.push(result);
    recoveredByFingerprint.set(fingerprint, grouped);
  }

  const resolvedModelIds = await Promise.all(
    [...recoveredByFingerprint.entries()].map(async ([fingerprint, grouped]) => {
      const activeModelIds = activeModelsByFingerprint.get(fingerprint) ?? [];
      const stormIncident = fingerprint.startsWith("status-monitor:storm:");
      if (stormIncident && grouped.length !== activeModelIds.length) {
        return {
          modelIds: [],
          completionStatements: [] as D1PreparedStatement[],
        };
      }
      if (stormIncident) {
        const event = modelsRecoveredEvent(config, grouped, fingerprint);
        return {
          modelIds: (await deliverAlert(env, event)) ? activeModelIds : [],
          completionStatements: [] as D1PreparedStatement[],
        };
      }
      const result = grouped[0];
      const delivery = await deliverModelAlert(
        env,
        result,
        "resolved",
        threshold,
        defaultOwner,
        modelRecoveredEvent(config, result, fingerprint),
      );
      return {
        modelIds: delivery.delivered ? activeModelIds : [],
        completionStatements: delivery.completionStatements,
      };
    }),
  );
  for (const resolved of resolvedModelIds) {
    for (const modelId of resolved.modelIds) {
      delete nextState[modelId];
    }
    completionStatements.push(...resolved.completionStatements);
  }

  const stateChanged = JSON.stringify(nextState) !== JSON.stringify(prevState);
  if (stateChanged || completionStatements.length > 0) {
    // D1 executes a batch transactionally. Keep the retry record and owner until
    // the corresponding alert-state transition is durable.
    await env.DB.batch([
      ...(stateChanged ? [prepareAlertStateWrite(env.DB, nextState)] : []),
      ...completionStatements,
    ]);
  }
}

/**
 * Edge-triggered alert for a *cycle-level* failure — the gateway being
 * unreachable (model discovery failed) or the prober key being rejected
 * account-wide. These paths return before any model is probed, so the per-model
 * alerter never runs; without this, the most severe outages would be silent.
 * Pages once on the transition to unhealthy and once on recovery, with
 * the same deliver-before-commit guarantee as the per-model path. With no
 * configured destination the down edge reaches deliverAlert's undeliverable
 * report and retries next cycle — never a silent no-op at the door, since a
 * cycle-level outage is the most severe alert class this worker emits.
 */
export async function runCycleAlert(env: Env, config: Config, status: CycleStatus): Promise<void> {
  const alerted = (await readCycleAlertState(env.DB)) != null;
  if (!status.ok) {
    if (!alerted && (await deliverAlert(env, cycleEvent(config, status)))) {
      await writeCycleAlertState(env.DB, status.checkedAt || "alerted");
    }
  } else if (alerted && (await deliverAlert(env, cycleEvent(config, status)))) {
    await writeCycleAlertState(env.DB, null);
  }
}
