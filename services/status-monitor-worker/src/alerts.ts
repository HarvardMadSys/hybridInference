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

/** Which models to page this cycle, plus the carried-over alert state. */
export interface AlertDecision {
  /** Models that newly crossed the failure threshold and should page as down. */
  down: ProbeResult[];
  /** Models that came back up after a down alert and should page as recovered. */
  recovered: ProbeResult[];
  /**
   * `prevState` minus any model no longer probed this cycle (so state can't grow
   * without bound). The down/recovery transitions are intentionally NOT applied
   * here — the caller commits them only after a confirmed Slack delivery.
   */
  baseState: Record<string, string>;
}

/**
 * Edge-triggered alert decision.
 *
 * `failing` holds the models whose most recent `threshold` probes were all
 * failures. `prevState` maps a model to the ISO time we last alerted it is down;
 * its presence means we've already paged for the current outage. A model is
 * paged *down* only on the transition into the failing set (so a sustained
 * outage pages once, not every 20-minute cron), and *recovered* only when a
 * probe succeeds after a down alert.
 *
 * This function is pure: it decides *what* to send but does not record that it
 * was sent. {@link runAlerts} applies the state transition only for an alert
 * whose POST actually succeeded, so a Slack outage retries next cycle instead of
 * silently dropping the page.
 */
export function decideAlerts(
  results: ProbeResult[],
  failing: Set<string>,
  prevState: Record<string, string>,
): AlertDecision {
  const baseState: Record<string, string> = { ...prevState };
  const present = new Set(results.map((r) => r.modelId));
  for (const id of Object.keys(baseState)) {
    if (!present.has(id)) delete baseState[id];
  }

  const down: ProbeResult[] = [];
  const recovered: ProbeResult[] = [];
  for (const r of results) {
    const alerted = baseState[r.modelId] != null;
    if (failing.has(r.modelId)) {
      if (!alerted) down.push(r);
    } else if (r.ok && alerted) {
      recovered.push(r);
    }
  }
  return { down, recovered, baseState };
}

/**
 * Evaluates probe results and sends Slack alerts for models that failed
 * `config.alertFailureThreshold` consecutive probes (and recovery notices for
 * those that come back). No-op when `SLACK_WEBHOOK_URL` is unset, so the feature
 * is opt-in via a single secret.
 *
 * Runs inside the probe cycle while it holds the cycle lock, so the
 * read-modify-write of the alert state is never raced by an overlapping cron.
 * A model is only recorded as alerted once its page is confirmed delivered, and
 * its state is only cleared once its recovery notice is delivered — so a Slack
 * webhook outage causes a retry on the next cycle rather than a lost alert.
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
  const { down, recovered, baseState } = decideAlerts(results, failing, prevState);

  const nextState: Record<string, string> = { ...baseState };

  // Send pages concurrently so a batch of simultaneous outages doesn't hold the
  // cycle lock for (count × per-request timeout). Each state transition is
  // committed only for a page that actually delivered: a failed POST leaves a
  // down model un-alerted (retried next cycle) and keeps a recovering model's
  // down state (recovery retried next cycle), so a Slack hiccup never drops an
  // alert instead of just delaying it.
  const downSent = await Promise.all(
    down.map(async (r) => ({
      modelId: r.modelId,
      checkedAt: r.checkedAt,
      ok: await postSlack(webhookUrl, formatModelDownAlert(config, r, threshold)),
    })),
  );
  for (const r of downSent) {
    if (r.ok) nextState[r.modelId] = r.checkedAt;
  }

  const recoveredSent = await Promise.all(
    recovered.map(async (r) => ({
      modelId: r.modelId,
      ok: await postSlack(webhookUrl, formatModelRecoveredAlert(config, r)),
    })),
  );
  for (const r of recoveredSent) {
    if (r.ok) delete nextState[r.modelId];
  }

  if (JSON.stringify(nextState) !== JSON.stringify(prevState)) {
    await writeAlertState(env.DB, nextState);
  }
}
