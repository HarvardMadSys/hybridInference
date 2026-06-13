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
}

function intOr(value: string | undefined, fallback: number): number {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/** Builds {@link Config} from raw environment bindings. */
export function loadConfig(env: Env): Config {
  return {
    gatewayBaseUrl: (env.GATEWAY_BASE_URL || "https://freeinference.org").replace(/\/+$/, ""),
    probePrompt: env.PROBE_PROMPT || "Write a short Python function that returns hello world.",
    probeMaxTokens: intOr(env.PROBE_MAX_TOKENS, 32),
    maxConcurrency: intOr(env.MAX_CONCURRENCY, 3),
    probeHeader: env.PROBE_HEADER || null,
    retentionDays: intOr(env.RETENTION_DAYS, 7),
    // Absolute per-probe deadline; SSE keepalives can otherwise keep a stalled
    // stream open indefinitely with no per-read timeout to trip.
    probeDeadlineMs: intOr(env.PROBE_DEADLINE_MS, 60_000),
  };
}
