import type { StatusMonitorControlPlaneService } from "./control-plane";

/** Bindings and configuration provided by the Workers runtime. */
export interface Env {
  DB: D1Database;
  PROBER_API_KEY: string;
  GATEWAY_BASE_URL: string;
  CF_VERSION_METADATA: WorkerVersionMetadata;
  ALERT_CONTROL_PLANE: StatusMonitorControlPlaneService;
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
  // Legacy rollback mode only (ALERT_DEFAULT_OWNER=legacy): more than this
  // many models changing state in one cycle collapses into a single summary
  // Slack message. Control-plane-owned incidents always open one per model
  // (D1 decision, 2026-07-27). Defaults to 5.
  ALERT_STORM_THRESHOLD?: string;
  // New individual model incidents use this writer. Existing incidents remain
  // pinned to the writer that opened them until their recovery is confirmed.
  ALERT_DEFAULT_OWNER?: string;
  // Writer for NEW cycle-level (gateway-down) incidents. Deliberately separate
  // from ALERT_DEFAULT_OWNER: the deployed control plane must accept
  // `monitoring_cycle_failure` before this flips, and status-monitor
  // auto-deploys from dev while the control plane deploys manually — a shared
  // flag would open a window where cycle events are emitted and rejected.
  // Unset means legacy.
  ALERT_CYCLE_OWNER?: string;
}

/** Local/dev gateway hosts that never indicate a real deployment. */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"]);

/**
 * Exact hosts, not patterns.
 *
 * This started as a display detail, where a loose suffix match cost nothing.
 * It is now the proof a deploy submits for what a Worker probes, so the same
 * looseness would let `notfreeinference.org` attest as production and any host
 * containing "staging" attest as staging. An allowlist has the property the
 * matching never did: a host nobody has vouched for is `unknown`, and the
 * deploy refuses it.
 */
const DEPLOYMENT_HOSTS: ReadonlyMap<string, string> = new Map([
  ["freeinference.org", "production"],
  ["staging.freeinference.org", "staging"],
]);

/**
 * The deployment this Worker probes, derived from the gateway it is configured
 * to call.
 *
 * This lives beside {@link Config} rather than beside the alerting that first
 * needed it because it is now the single answer to "which deployment is this
 * Worker about" — used to label probe rows, to scope health reads, and to prove
 * the target at deploy time. Deriving it from the configured URL is what keeps
 * those from drifting apart when the URL moves.
 */
export function deriveEnvironment(gatewayBaseUrl: string): string {
  let url: URL;
  try {
    url = new URL(gatewayBaseUrl);
  } catch {
    return "unknown";
  }
  // `.hostname` (not `.host`) excludes the port and keeps IPv6 brackets intact,
  // so `freeinference.org:8443` still matches and `[::1]` isn't truncated.
  const hostname = url.hostname.toLowerCase();
  if (!hostname || LOCAL_HOSTS.has(hostname)) return "local";
  // A deployment we page for is reached over TLS. Probing one over plaintext
  // would be measuring something else, and attesting it as that deployment
  // would put a name on the difference.
  if (url.protocol !== "https:") return "unknown";
  return DEPLOYMENT_HOSTS.get(hostname) ?? "unknown";
}

/** Normalized configuration derived from {@link Env}. */
export interface Config {
  gatewayBaseUrl: string;
  /** Which deployment this Worker's probes measure. Never assumed — derived. */
  targetEnvironment: string;
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
  const gatewayBaseUrl = (env.GATEWAY_BASE_URL || "https://freeinference.org").replace(
    /\/+$/,
    "",
  );
  return {
    gatewayBaseUrl,
    targetEnvironment: deriveEnvironment(gatewayBaseUrl),
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
    // Legacy rollback mode only: above this many models changing state in one
    // cycle, legacy-owned pages collapse into a single summary message so a
    // provider-wide blip doesn't flood the webhook channel. Control-plane
    // incidents always open per model.
    alertStormThreshold: intOr(env.ALERT_STORM_THRESHOLD, 5),
  };
}
