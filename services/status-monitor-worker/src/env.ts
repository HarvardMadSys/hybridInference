/** Bindings and configuration provided by the Workers runtime. */
export interface Env {
  DB: D1Database;
  PROBER_API_KEY: string;
  GATEWAY_BASE_URL: string;
  PROBE_PROMPT?: string;
  PROBE_MAX_TOKENS?: string;
  MAX_CONCURRENCY?: string;
  PROBE_HEADER?: string;
  RETENTION_DAYS?: string;
  PROBE_DEADLINE_MS?: string;
  // Slack incoming-webhook URL (a secret, not a var). Unset disables direct
  // Slack delivery.
  SLACK_WEBHOOK_URL?: string;
  // Optional Codex oncall relay. Both values are required to enable relay delivery.
  CODEX_ONCALL_RELAY_URL?: string;
  CODEX_ONCALL_RELAY_TOKEN?: string;
  // Consecutive failed probes before a model pages Slack. Defaults to 2.
  ALERT_FAILURE_THRESHOLD?: string;
  // More than this many models changing state in one cycle collapses into a
  // single summary Slack message instead of one per model. Defaults to 5.
  ALERT_STORM_THRESHOLD?: string;
}

/** Normalized configuration derived from {@link Env}. */
export interface Config {
  gatewayBaseUrl: string;
  probePrompt: string;
  probeMaxTokens: number;
  maxConcurrency: number;
  probeHeader: string | null;
  retentionDays: number;
  probeDeadlineMs: number;
  alertFailureThreshold: number;
  alertStormThreshold: number;
}

function intOr(value: string | undefined, fallback: number): number {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/** Builds {@link Config} from raw environment bindings. */
export function loadConfig(env: Env): Config {
  return {
    gatewayBaseUrl: (env.GATEWAY_BASE_URL || "https://freeinference.org").replace(/\/+$/, ""),
    probePrompt:
      env.PROBE_PROMPT ||
      "Write a Python function that implements binary search over a sorted list. " +
        "Include a docstring, type hints, and a short example of calling it.",
    probeMaxTokens: intOr(env.PROBE_MAX_TOKENS, 1024),
    maxConcurrency: intOr(env.MAX_CONCURRENCY, 3),
    probeHeader: env.PROBE_HEADER || null,
    retentionDays: intOr(env.RETENTION_DAYS, 7),
    // Absolute per-probe deadline; SSE keepalives can otherwise keep a stalled
    // stream open indefinitely with no per-read timeout to trip.
    probeDeadlineMs: intOr(env.PROBE_DEADLINE_MS, 60_000),
    // Consecutive failed probes that page Slack. Two suppresses a single
    // transient blip from alerting; intOr floors invalid/≤0 values at the default.
    alertFailureThreshold: intOr(env.ALERT_FAILURE_THRESHOLD, 2),
    // Above this many models changing state in one cycle, pages collapse into a
    // single summary message so a provider-wide blip doesn't flood the channel.
    alertStormThreshold: intOr(env.ALERT_STORM_THRESHOLD, 5),
  };
}
