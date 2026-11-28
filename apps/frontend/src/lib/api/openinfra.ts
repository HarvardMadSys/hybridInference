// Public (unauthenticated) client for the live metrics behind /openinfra, and
// the admin-only export of their history.

import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from '@/lib/api/client';

const API_BASE = config.apiBase;

/** A row's health; `offline` once nothing has been heard from it for 90 seconds. */
export type OpenInfraStatus = 'online' | 'degraded' | 'offline' | 'unknown';

/** The whole fleet's health, or one deployment's as the page computes it. */
export type FleetHealth = 'healthy' | 'busy' | 'degraded' | 'offline';

/** A server that reports, with its host CPU and memory. */
export interface OpenInfraNode {
  id: string;
  hostname: string;
  region: string | null;
  cluster: string | null;
  status: OpenInfraStatus;
  last_seen_at: number;
  cpu_util_pct: number | null;
  load1: number | null;
  load5: number | null;
  load15: number | null;
  mem_used_mb: number | null;
  mem_total_mb: number | null;
  mem_available_mb: number | null;
  swap_used_mb: number | null;
  procs_running: number | null;
  procs_total: number | null;
  cpu_count: number | null;
  gpu_count: number;
  avg_utilization_pct: number | null;
}

/** One GPU, by its node and index (`id` is `<node_id>:<index>`). */
export interface OpenInfraGpu {
  id: string;
  node_id: string;
  hostname: string;
  region: string | null;
  cluster: string | null;
  gpu_index: number;
  uuid: string | null;
  name: string | null;
  driver: string | null;
  memory_total_mb: number | null;
  utilization_pct: number | null;
  memory_util_pct: number | null;
  memory_used_mb: number | null;
  temperature_c: number | null;
  power_w: number | null;
  power_limit_w: number | null;
  fan_speed_pct: number | null;
  clocks_sm_mhz: number | null;
  clocks_mem_mhz: number | null;
  ecc_errors: number | null;
  pstate: string | null;
  processes: unknown;
  status: OpenInfraStatus;
  last_seen_at: number;
  tensor_active: number | null;
  dram_active: number | null;
  pcie_tx_bps: number | null;
  pcie_rx_bps: number | null;
  nvlink_tx_bps: number | null;
  nvlink_rx_bps: number | null;
}

/**
 * One inference engine (a deployment): SGLang or vLLM, named by `engine`. The
 * document keeps its historical key, `sglang`, for the list of them.
 */
export interface OpenInfraEngine {
  id: string;
  node_id: string | null;
  hostname: string | null;
  region: string | null;
  name: string | null;
  model_name: string | null;
  endpoint: string | null;
  engine: string | null;
  status: OpenInfraStatus;
  /** Comma-separated indexes of the node's GPUs this engine runs on. */
  gpu_indexes: string | null;
  last_seen_at: number;
  num_running_reqs: number | null;
  num_queue_reqs: number | null;
  num_used_tokens: number | null;
  token_usage: number | null;
  gen_throughput: number | null;
  cache_hit_rate: number | null;
  prompt_tokens_total: number | null;
  generation_tokens_total: number | null;
  ttft_avg_s: number | null;
  queue_avg_s: number | null;
  prefill_avg_s: number | null;
  e2e_latency_avg_s: number | null;
  tpot_avg_s: number | null;
  sm_util_pct: number | null;
  max_total_num_tokens: number | null;
  num_active_tokens: number | null;
  num_waiting_uncached_tokens: number | null;
  prefill_tps: number | null;
  spec_accept_length: number | null;
  sglang_util_pct: number | null;
  max_running_requests: number | null;
  weight_gb: number | null;
  kv_cache_gb: number | null;
  spec_accept_rate: number | null;
  req_per_min: number | null;
  cached_prefill_tps: number | null;
  chunked_prefill_avg_s: number | null;
  hicache_host_used: number | null;
  hicache_host_total: number | null;
  device_cached_tps: number | null;
  host_cached_tps: number | null;
  loadback_tps: number | null;
  evict_tps: number | null;
  backup_tps: number | null;
  prompt_tokens_avg: number | null;
  uncached_prompt_tokens_avg: number | null;
  generation_tokens_avg: number | null;
  http_error_per_min: number | null;
  retracts: number | null;
}

/** Requests and tokens an engine delivered over one window. */
export interface ServedBucket {
  reqs: number;
  cache_tokens: number;
  prefill_tokens: number;
  decode_tokens: number;
}

export interface ServedServer {
  id: string;
  name: string | null;
  hour: ServedBucket;
  day: ServedBucket;
}

/** One finished request's shape and latency, from the engine's log. No content. */
export interface OpenInfraRequestEvent {
  server_id: string;
  ts: number;
  model: string | null;
  input_len: number | null;
  output_len: number | null;
  cache_len: number | null;
  uncached_len: number | null;
  ttft_s: number | null;
  queue_s: number | null;
  prefill_s: number | null;
  tpot_s: number | null;
  e2e_s: number | null;
  decode_tps: number | null;
  spec_accept_len: number | null;
  spec_accept_rate: number | null;
  batch_size: number | null;
}

/** Fleet-wide figures the gateway computes; the page recomputes them per deployment. */
export interface OpenInfraKpis {
  nodes: number;
  nodes_online: number;
  gpus: number;
  gpus_online: number;
  avg_utilization_pct: number | null;
  tensor_active: number | null;
  dram_active: number | null;
  nvlink_gbps: number | null;
  memory_used_mb: number | null;
  memory_total_mb: number | null;
  avg_temperature_c: number | null;
  power_w: number | null;
  sglang_servers: number;
  sglang_online: number;
  running_reqs: number | null;
  queue_reqs: number | null;
  gen_throughput: number | null;
  token_usage: number | null;
  cache_hit_rate: number | null;
  used_tokens: number | null;
  max_tokens: number | null;
  active_tokens: number | null;
  waiting_tokens: number | null;
  prefill_tps: number | null;
  cpu_util_pct: number | null;
  load1: number | null;
  ram_used_mb: number | null;
  ram_total_mb: number | null;
}

/** `GET /public-openinfra`: the current snapshot. Times are milliseconds since the epoch. */
export interface OpenInfraDashboard {
  generated_at: number;
  fleet: FleetHealth;
  kpis: OpenInfraKpis;
  nodes: OpenInfraNode[];
  gpus: OpenInfraGpu[];
  /** The engines, already in the deployment's tab order. */
  sglang: OpenInfraEngine[];
  served: { hour: ServedBucket; day: ServedBucket; servers: ServedServer[] };
  requests: OpenInfraRequestEvent[];
}

/** One point of one GPU, engine or node. Its fields depend on which. */
export interface OpenInfraHistoryRow {
  ts: number;
  [field: string]: number | string | null;
}

/** `GET /public-openinfra/history`: one point per series and `grain_ms`. */
export interface OpenInfraHistory {
  generated_at: number;
  grain_ms: number;
  gpu: OpenInfraHistoryRow[];
  sglang: OpenInfraHistoryRow[];
  cpu: OpenInfraHistoryRow[];
}

export type OpenInfraResult<T> =
  | { status: 'ok'; data: T }
  | { status: 'unavailable' }
  | { status: 'error' };

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isDashboard(value: unknown): value is OpenInfraDashboard {
  return (
    isObject(value) &&
    typeof value.generated_at === 'number' &&
    isObject(value.kpis) &&
    Array.isArray(value.nodes) &&
    Array.isArray(value.gpus) &&
    Array.isArray(value.sglang) &&
    isObject(value.served) &&
    Array.isArray((value.served as Record<string, unknown>).servers) &&
    Array.isArray(value.requests)
  );
}

function isHistory(value: unknown): value is OpenInfraHistory {
  return (
    isObject(value) &&
    typeof value.grain_ms === 'number' &&
    Array.isArray(value.gpu) &&
    Array.isArray(value.sglang) &&
    Array.isArray(value.cpu)
  );
}

async function getDocument<T>(
  path: string,
  valid: (value: unknown) => value is T,
): Promise<OpenInfraResult<T>> {
  try {
    const resp = await fetch(`${API_BASE}${path}`, { headers: { Accept: 'application/json' } });
    // The deployment does not publish the page.
    if (resp.status === 404) return { status: 'unavailable' };
    if (!resp.ok) return { status: 'error' };
    const body: unknown = await resp.json();
    return valid(body) ? { status: 'ok', data: body } : { status: 'error' };
  } catch {
    return { status: 'error' };
  }
}

/**
 * Fetch the current snapshot.
 *
 * Resolves to `unavailable` when the deployment does not publish OpenInfra and
 * to `error` on any other failure, so the page can say which it is.
 */
export function getOpenInfraDashboard(): Promise<OpenInfraResult<OpenInfraDashboard>> {
  return getDocument('/public-openinfra', isDashboard);
}

/** Fetch the history of the last `hours` hours, with the same three outcomes. */
export function getOpenInfraHistory(hours: number): Promise<OpenInfraResult<OpenInfraHistory>> {
  const query = new URLSearchParams({ hours: String(hours) });
  return getDocument(`/public-openinfra/history?${query.toString()}`, isHistory);
}

/** How many days of history an administrator may export. */
export const EXPORT_DAYS = [7, 14, 30] as const;
export type ExportDays = (typeof EXPORT_DAYS)[number];

/**
 * Download the last `days` days of hourly rollups, the snapshot and the served
 * totals as one JSON file. Administrators only: the request goes as this tab's
 * session, and a refusal is thrown as the gateway's error.
 *
 * Returns the file name and contents; the caller hands them to the browser.
 */
export async function exportOpenInfra(days: ExportDays): Promise<{ filename: string; blob: Blob }> {
  const resp = await fetchWithAuth(API_BASE, `/admin/openinfra/export?days=${days}`, {
    cache: 'no-store',
  });
  if (!resp.ok) await jsonOrThrow(resp);
  const blob = await resp.blob();
  // Hidden from a script when the gateway is on another origin; then the
  // default name stands.
  const match = /filename="([^"]+)"/.exec(resp.headers.get('Content-Disposition') ?? '');
  return { filename: match ? match[1] : `openinfra-export-${days}d.json`, blob };
}
