import { modelsFailingStreak, readAlertState, writeAlertState } from "./db";
import type { Config, Env } from "./env";
import type { ProbeResult } from "./probe";

/** Local/dev gateway hosts that never indicate a real deployment. */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"]);

/**
 * Best-effort deployment environment for the gateway the probes run against,
 * derived from its host (mirrors the backend's alert environment detection).
 */
export function deriveEnvironment(gatewayBaseUrl: string): string {
  let host: string;
  try {
    host = new URL(gatewayBaseUrl).host.toLowerCase();
  } catch {
    return "unknown";
  }
  const hostname = host.split(":")[0];
  if (!hostname || LOCAL_HOSTS.has(hostname)) return "local";
  if (host.includes("staging")) return "staging";
  if (host.endsWith("freeinference.org")) return "production";
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
  } catch (err) {
    console.error("slack webhook post failed", err);
    return false;
  }
}

/** The set of alerts to send this cycle plus the alert state to persist. */
export interface AlertDecision {
  down: ProbeResult[];
  recovered: ProbeResult[];
  nextState: Record<string, string>;
}

/**
 * Edge-triggered alert decision.
 *
 * `failing` holds the models whose most recent `threshold` probes were all
 * failures. `prevState` maps a model to the ISO time we last alerted it is down;
 * its presence means we've already paged for the current outage. A model is
 * alerted *down* only on the transition into the failing set (so a sustained
 * outage pages once, not every 20-minute cron), and alerted *recovered* only
 * when a probe succeeds after a down alert. State for models no longer probed
 * this cycle is dropped so it can't grow without bound.
 */
export function decideAlerts(
  results: ProbeResult[],
  failing: Set<string>,
  prevState: Record<string, string>,
): AlertDecision {
  const nextState: Record<string, string> = { ...prevState };
  const down: ProbeResult[] = [];
  const recovered: ProbeResult[] = [];
  for (const r of results) {
    const alerted = nextState[r.modelId] != null;
    if (failing.has(r.modelId)) {
      if (!alerted) {
        down.push(r);
        nextState[r.modelId] = r.checkedAt;
      }
    } else if (r.ok && alerted) {
      recovered.push(r);
      delete nextState[r.modelId];
    }
  }
  const present = new Set(results.map((r) => r.modelId));
  for (const id of Object.keys(nextState)) {
    if (!present.has(id)) delete nextState[id];
  }
  return { down, recovered, nextState };
}

/**
 * Evaluates probe results and sends Slack alerts for models that failed
 * `config.alertFailureThreshold` consecutive probes (and recovery notices for
 * those that come back). No-op when `SLACK_WEBHOOK_URL` is unset, so the feature
 * is opt-in via a single secret.
 *
 * Runs inside the probe cycle while it holds the cycle lock, so the
 * read-modify-write of the alert state is never raced by an overlapping cron.
 */
export async function runAlerts(env: Env, config: Config, results: ProbeResult[]): Promise<void> {
  const webhookUrl = env.SLACK_WEBHOOK_URL;
  if (!webhookUrl || results.length === 0) return;

  const threshold = config.alertFailureThreshold;
  // Only a model that failed *this* cycle can newly cross the threshold; limiting
  // the streak lookup to those keeps D1 rows_read at threshold × (failed models).
  const failedNow = results.filter((r) => !r.ok).map((r) => r.modelId);
  const failing = await modelsFailingStreak(env.DB, failedNow, threshold);
  const prevState = await readAlertState(env.DB);
  const { down, recovered, nextState } = decideAlerts(results, failing, prevState);

  for (const r of down) {
    await postSlack(webhookUrl, formatModelDownAlert(config, r, threshold));
  }
  for (const r of recovered) {
    await postSlack(webhookUrl, formatModelRecoveredAlert(config, r));
  }

  if (JSON.stringify(nextState) !== JSON.stringify(prevState)) {
    await writeAlertState(env.DB, nextState);
  }
}
