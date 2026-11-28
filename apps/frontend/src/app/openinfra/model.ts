// The page's arithmetic, without React: which deployment is shown, its slice
// of the snapshot and of the history, and the figures drawn from them.

import type {
  FleetHealth,
  OpenInfraDashboard,
  OpenInfraEngine,
  OpenInfraHistory,
  OpenInfraHistoryRow,
  OpenInfraKpis,
  ServedBucket,
} from '@/lib/api/openinfra';

/** The time ranges the page offers, and the spacing of the points each draws. */
export const RANGES = [
  { hours: 0.25, grain: 'five_seconds' },
  { hours: 2, grain: 'minute' },
  { hours: 24, grain: 'twenty_minutes' },
  { hours: 168, grain: 'hour' },
] as const;

export const DEFAULT_HOURS = 2;

/** The history rows of one view: GPUs, engines and hosts. */
export interface HistoryRows {
  gpu: OpenInfraHistoryRow[];
  sglang: OpenInfraHistoryRow[];
  cpu: OpenInfraHistoryRow[];
}

export const NO_HISTORY: HistoryRows = { gpu: [], sglang: [], cpu: [] };

/** A finite number, or null: history values may arrive as strings or nulls. */
export function toNum(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const x = Number(value);
  return Number.isFinite(x) ? x : null;
}

export function mean(values: Array<number | null | undefined>): number | null {
  const xs = values.filter((v): v is number => typeof v === 'number' && !Number.isNaN(v));
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
}

export function sum(values: Array<number | null | undefined>): number {
  return values.reduce<number>(
    (a, v) => a + (typeof v === 'number' && !Number.isNaN(v) ? v : 0),
    0,
  );
}

/** The `?hours=` the address asks for: one of the ranges, or any span from 15 minutes to 7 days. */
export function hoursFromSearch(search: string): number {
  const raw = new URLSearchParams(search).get('hours');
  const n = raw === null || raw.trim() === '' ? NaN : Number(raw);
  if (RANGES.some((r) => r.hours === n)) return n;
  if (Number.isFinite(n) && n >= 0.25 && n <= 168) return n;
  return DEFAULT_HOURS;
}

/** The deployment the address names after `#`; '' for none or the old `#fleet`. */
export function deployFromHash(hash: string): string {
  let h = hash.replace(/^#/, '');
  try {
    h = decodeURIComponent(h);
  } catch {
    // keep the raw hash
  }
  return !h || h === 'fleet' ? '' : h;
}

export function parseIndexes(raw: unknown): number[] {
  if (raw === null || raw === undefined || raw === '') return [];
  if (Array.isArray(raw)) return raw.map(Number).filter((x) => Number.isFinite(x));
  return String(raw)
    .split(/[,\s]+/)
    .filter(Boolean)
    .map(Number)
    .filter((x) => Number.isFinite(x));
}

function sameDeploy(server: OpenInfraEngine, id: string): boolean {
  return server.id === id || server.name === id;
}

export function deployKey(server: OpenInfraEngine): string {
  return server.name || server.id;
}

/**
 * One tab per deployment name, in the order the snapshot lists them. A retired
 * host can keep an offline row under the name of an engine that moved; the tab
 * follows the one still reporting.
 */
export function preferLive(servers: readonly OpenInfraEngine[]): OpenInfraEngine[] {
  const byName = new Map<string, OpenInfraEngine>();
  for (const server of servers) {
    const key = deployKey(server);
    const prev = byName.get(key);
    if (!prev || (prev.status === 'offline' && server.status !== 'offline')) {
      byName.set(key, server);
    }
  }
  return [...byName.values()];
}

export function findDeploy(data: OpenInfraDashboard | null, id: string): OpenInfraEngine | null {
  if (!data || !id) return null;
  const matches = data.sglang.filter((server) => sameDeploy(server, id));
  if (!matches.length) return null;
  return matches.find((server) => server.status !== 'offline') ?? matches[0];
}

/** The deployment to show: the one the address names if it exists, else the first tab. */
export function resolveDeploy(data: OpenInfraDashboard, selected: string): string {
  if (selected && findDeploy(data, selected)) return selected;
  const first = preferLive(data.sglang)[0];
  return first ? deployKey(first) : '';
}

/** The snapshot as one deployment sees it: its node, its GPUs, itself. */
export interface DashboardView {
  nodes: OpenInfraDashboard['nodes'];
  gpus: OpenInfraDashboard['gpus'];
  sglang: OpenInfraEngine[];
  kpis: ViewKpis;
  served: OpenInfraDashboard['served'];
  requests: OpenInfraDashboard['requests'];
}

/** The snapshot's figures, plus the rates a deployment's tiles add. */
export interface ViewKpis extends OpenInfraKpis {
  cached_prefill_tps?: number | null;
  req_per_min?: number | null;
  ttft_avg_s?: number | null;
}

export function filterDash(data: OpenInfraDashboard, deployId: string): DashboardView {
  const all: DashboardView = { ...data, kpis: data.kpis };
  if (!deployId) return all;
  const server = findDeploy(data, deployId);
  if (!server) return all;
  const indexes = parseIndexes(server.gpu_indexes);
  const gpus = data.gpus.filter(
    (g) => g.node_id === server.node_id && indexes.includes(Number(g.gpu_index)),
  );
  const sglang = [server];
  const nodes = data.nodes.filter((item) => item.id === server.node_id);
  const onlineGpus = gpus.filter((g) => g.status !== 'offline');
  const onlineServers = sglang.filter((s) => s.status !== 'offline');
  return {
    ...data,
    nodes,
    gpus,
    sglang,
    kpis: {
      ...data.kpis,
      nodes: nodes.length,
      nodes_online: nodes.filter((item) => item.status === 'online').length,
      gpus: gpus.length,
      gpus_online: onlineGpus.length,
      avg_utilization_pct: mean(onlineGpus.map((g) => g.utilization_pct)),
      tensor_active: mean(onlineGpus.map((g) => g.tensor_active)),
      memory_used_mb: sum(gpus.map((g) => g.memory_used_mb)),
      memory_total_mb: sum(gpus.map((g) => g.memory_total_mb)),
      power_w: sum(onlineGpus.map((g) => g.power_w)),
      sglang_servers: sglang.length,
      running_reqs: sum(sglang.map((s) => s.num_running_reqs)),
      queue_reqs: sum(sglang.map((s) => s.num_queue_reqs)),
      used_tokens: sum(sglang.map((s) => s.num_used_tokens)),
      max_tokens: sum(sglang.map((s) => s.max_total_num_tokens)),
      prefill_tps: sum(onlineServers.map((s) => s.prefill_tps)),
      gen_throughput: sum(onlineServers.map((s) => s.gen_throughput)),
      cached_prefill_tps: sum(onlineServers.map((s) => s.cached_prefill_tps)),
      req_per_min: sum(onlineServers.map((s) => s.req_per_min)),
      cache_hit_rate: mean(onlineServers.map((s) => s.cache_hit_rate)),
      ttft_avg_s: mean(onlineServers.map((s) => s.ttft_avg_s)),
      token_usage: mean(onlineServers.map((s) => s.token_usage)),
      cpu_util_pct: mean(nodes.map((item) => item.cpu_util_pct)),
      load1: mean(nodes.map((item) => item.load1)),
      ram_used_mb: sum(nodes.map((item) => item.mem_used_mb)),
      ram_total_mb: sum(nodes.map((item) => item.mem_total_mb)),
    },
    requests: data.requests.filter((r) => r.server_id === server.id),
  };
}

/** The history rows of the deployment shown: its node's host, its GPUs, itself. */
export function filterHist(
  hist: HistoryRows,
  data: OpenInfraDashboard | null,
  deployId: string,
): HistoryRows {
  if (!deployId) return hist;
  const server = findDeploy(data, deployId);
  if (!server) return NO_HISTORY;
  const indexes = new Set(parseIndexes(server.gpu_indexes));
  return {
    gpu: hist.gpu.filter((r) => r.node_id === server.node_id && indexes.has(Number(r.gpu_index))),
    sglang: hist.sglang.filter((r) => r.server_id === server.id),
    cpu: hist.cpu.filter((r) => r.node_id === server.node_id),
  };
}

export function historyRows(hist: OpenInfraHistory | null): HistoryRows {
  return hist ? { gpu: hist.gpu, sglang: hist.sglang, cpu: hist.cpu } : NO_HISTORY;
}

/** The view's health: offline with no GPU reporting, then degraded, busy or healthy. */
export function viewFleet(view: DashboardView): FleetHealth {
  const onlineGpus = view.gpus.filter((g) => g.status !== 'offline');
  const queued = view.kpis.queue_reqs || 0;
  const avgU = view.kpis.avg_utilization_pct || 0;
  if (onlineGpus.length === 0) return 'offline';
  if (view.gpus.some((g) => g.status === 'degraded') || queued > 1500) return 'degraded';
  if (queued > 200 || avgU > 80) return 'busy';
  return 'healthy';
}

/**
 * Prompt (cached and computed) plus generated tokens per second, or null with
 * none of them. Takes an engine, a history row or the tiles' figures.
 */
export function prefillDecodeTps(row: object): number | null {
  const rates = row as {
    prefill_tps?: unknown;
    cached_prefill_tps?: unknown;
    gen_throughput?: unknown;
  };
  const p = toNum(rates.prefill_tps);
  const c = toNum(rates.cached_prefill_tps);
  const d = toNum(rates.gen_throughput);
  if (p === null && c === null && d === null) return null;
  return (p || 0) + (c || 0) + (d || 0);
}

export function perMin(value: unknown): number | null {
  const x = toNum(value);
  return x === null ? null : x * 60;
}

/** The tiles' figures: averages over the range shown where history has them, else the snapshot's. */
export interface RangeKpis extends ViewKpis {
  /** True when the figures are averages over the range, false when they are the snapshot. */
  averaged: boolean;
}

export function kpisFromHist(view: DashboardView, hist: HistoryRows): RangeKpis {
  const k = view.kpis;
  const { gpu, sglang: sg, cpu } = hist;
  const averaged = gpu.length + sg.length + cpu.length > 0;
  const avgF = (rows: OpenInfraHistoryRow[], key: string) => mean(rows.map((r) => toNum(r[key])));
  const avgHit = mean(
    sg.map((r) => toNum(r.cache_hit_rate)).filter((v): v is number => v !== null && v > 0),
  );
  const memPct = avgF(gpu, 'memory_pct');
  const ramUsed = avgF(cpu, 'mem_used_mb');
  const ramTotal = avgF(cpu, 'mem_total_mb') ?? k.ram_total_mb;
  return {
    ...k,
    averaged,
    cpu_util_pct: avgF(cpu, 'cpu_util_pct') ?? k.cpu_util_pct,
    load1: avgF(cpu, 'load1') ?? k.load1,
    ram_used_mb: ramUsed ?? k.ram_used_mb,
    ram_total_mb: ramTotal,
    avg_utilization_pct: avgF(gpu, 'utilization_pct') ?? k.avg_utilization_pct,
    tensor_active: avgF(gpu, 'tensor_active') ?? k.tensor_active,
    memory_used_mb:
      memPct !== null && k.memory_total_mb ? (memPct / 100) * k.memory_total_mb : k.memory_used_mb,
    queue_reqs: avgF(sg, 'queued') ?? k.queue_reqs,
    running_reqs: avgF(sg, 'running') ?? k.running_reqs,
    prefill_tps: avgF(sg, 'prefill_tps') ?? k.prefill_tps,
    gen_throughput: avgF(sg, 'gen_throughput') ?? k.gen_throughput,
    cached_prefill_tps: avgF(sg, 'cached_prefill_tps') ?? k.cached_prefill_tps,
    req_per_min: avgF(sg, 'req_per_min') ?? k.req_per_min,
    cache_hit_rate: avgHit ?? k.cache_hit_rate,
    ttft_avg_s: avgF(sg, 'ttft_avg_s') ?? k.ttft_avg_s,
    used_tokens: avgF(sg, 'used_tokens') ?? k.used_tokens,
    max_tokens: avgF(sg, 'max_tokens') ?? k.max_tokens,
    token_usage: avgF(sg, 'token_usage') ?? k.token_usage,
  };
}

const ZERO: ServedBucket = { reqs: 0, cache_tokens: 0, prefill_tokens: 0, decode_tokens: 0 };

function addServed(a: ServedBucket, b: Partial<ServedBucket> | undefined): ServedBucket {
  return {
    reqs: a.reqs + (b?.reqs || 0),
    cache_tokens: a.cache_tokens + (b?.cache_tokens || 0),
    prefill_tokens: a.prefill_tokens + (b?.prefill_tokens || 0),
    decode_tokens: a.decode_tokens + (b?.decode_tokens || 0),
  };
}

/** What the view's engines served over the last hour and the last 24 hours. */
export function servedTotals(view: DashboardView): { hour: ServedBucket; day: ServedBucket } {
  const ids = new Set(view.sglang.map((s) => s.id));
  const rows = view.served.servers.filter((row) => ids.has(row.id));
  return {
    hour: rows.reduce((acc, row) => addServed(acc, row.hour), { ...ZERO }),
    day: rows.reduce((acc, row) => addServed(acc, row.day), { ...ZERO }),
  };
}

/** Rows grouped by one field, in the order the history lists them. */
export function groupRows(
  rows: readonly OpenInfraHistoryRow[],
  key: string,
): Map<string, OpenInfraHistoryRow[]> {
  const groups = new Map<string, OpenInfraHistoryRow[]>();
  for (const row of rows) {
    const k = String(row[key] ?? 'all');
    const list = groups.get(k);
    if (list) list.push(row);
    else groups.set(k, [row]);
  }
  return groups;
}

/** Whether rows come from more than one node, so their series need the node's name. */
export function spansNodes(rows: readonly OpenInfraHistoryRow[]): boolean {
  return new Set(rows.map((r) => r.node_id).filter(Boolean)).size > 1;
}

/** A model's short name: the last segment of `org/model`. */
export function shortModel(name: string | null | undefined): string | null {
  if (!name) return null;
  return name.split('/').pop() || name;
}
