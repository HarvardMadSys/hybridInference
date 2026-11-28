// Every history chart the page draws: its title, unit and description, and the
// series it takes from the history rows of the deployment shown.

import type { OpenInfraHistoryRow } from '@/lib/api/openinfra';
import type { ChartOptions, ChartSeries } from './chartCanvas';
import type { InfraFormat } from './format';
import { groupRows, perMin, prefillDecodeTps, spansNodes, toNum, type HistoryRows } from './model';

/**
 * Series colors, checked against white and the dark console surface: the first
 * three are also the fixed roles a chart's parts take (a TTFT's queue, prefill
 * and total; a token mix's cache, prefill and decode).
 */
export const PALETTE = [
  '#2a78d6',
  '#eb6834',
  '#d6336c',
  '#1baf7a',
  '#7c5cdb',
  '#c99100',
  '#0f9fb3',
  '#8c6d4f',
] as const;

export const ROLE = { first: PALETTE[0], second: PALETTE[1], third: PALETTE[2] } as const;

export interface ChartSpec {
  id: string;
  title: string;
  unit: string;
  desc: string;
  series: ChartSeries[];
  opts: ChartOptions;
}

export interface ChartSections {
  host: ChartSpec[];
  gpu: ChartSpec[];
  engine: ChartSpec[];
}

type Transform = (value: OpenInfraHistoryRow[string], row: OpenInfraHistoryRow) => number | null;
type Labeler = (row: OpenInfraHistoryRow) => string;

function lines(
  groups: Map<string, OpenInfraHistoryRow[]>,
  field: string,
  transform: Transform | null,
  label: Labeler,
  start = 0,
  color?: string,
): ChartSeries[] {
  const out: ChartSeries[] = [];
  let i = start;
  for (const [key, rows] of groups) {
    out.push({
      key: `${key}:${field}`,
      label: label(rows[0]),
      color: color ?? PALETTE[i % PALETTE.length],
      points: rows.map((r) => ({
        t: Number(r.ts),
        v: toNum(transform ? transform(r[field], r) : r[field]),
      })),
    });
    i += 1;
  }
  return out;
}

/** A ratio sent as 0-1 (or already as a percent), as a percent. */
const asPercent: Transform = (v) => {
  const x = toNum(v);
  return x === null ? null : x <= 1.5 ? x * 100 : x;
};
const times100: Transform = (v) => {
  const x = toNum(v);
  return x === null ? null : x * 100;
};
const toGb: Transform = (v) => {
  const x = toNum(v);
  return x === null ? null : x / 1e9;
};
const times1000: Transform = (v) => {
  const x = toNum(v);
  return x === null ? null : x * 1000;
};

function peak(series: ChartSeries[]): number {
  let max = 0;
  for (const s of series) for (const p of s.points) if (p.v !== null && p.v > max) max = p.v;
  return max;
}

/** Build the three sections' charts from the history rows shown. */
export function buildCharts(hist: HistoryRows, f: InfraFormat): ChartSections {
  const { t } = f;
  const cpuG = groupRows(hist.cpu, 'node_id');
  const gpuG = groupRows(hist.gpu, 'gpu_id');
  const sgG = groupRows(hist.sglang, 'server_id');
  const cpuFleet = spansNodes(hist.cpu);
  const gpuFleet = spansNodes(hist.gpu);
  const sgFleet = spansNodes(hist.sglang);

  const word = {
    util: t('openinfra.series.util', 'util %'),
    load1: t('openinfra.series.load1', 'load1'),
    used: t('openinfra.series.used_gb', 'used GB'),
    running: t('openinfra.series.running', 'running'),
    queued: t('openinfra.series.queued', 'queued'),
    queue: t('openinfra.series.queue', 'queue'),
    prefill: t('openinfra.series.prefill', 'prefill'),
    ttft: t('openinfra.series.ttft', 'TTFT'),
    device: t('openinfra.series.device', 'device'),
    host: t('openinfra.series.host', 'host'),
    backup: t('openinfra.series.backup', 'backup'),
    loadback: t('openinfra.series.loadback', 'load-back'),
    evict: t('openinfra.series.evict', 'evict'),
    prompt: t('openinfra.series.prompt', 'prompt'),
    uncached: t('openinfra.series.uncached', 'uncached'),
    gen: t('openinfra.series.generation', 'generation'),
    tx: t('openinfra.series.tx', 'TX'),
    rx: t('openinfra.series.rx', 'RX'),
  };

  const join = (...parts: Array<string | null | undefined>) => parts.filter(Boolean).join(' ');
  const nodeName = (row: OpenInfraHistoryRow, extra: string): string =>
    cpuFleet ? join(String(row.node_id ?? ''), extra) : extra || String(row.node_id ?? '');
  const gpuName = (row: OpenInfraHistoryRow, extra = ''): string =>
    join(gpuFleet ? String(row.node_id ?? '') : '', `GPU ${row.gpu_index ?? '?'}`, extra);
  const serverName = (row: OpenInfraHistoryRow, extra = ''): string =>
    join(
      sgFleet && row.node_id ? String(row.node_id) : '',
      String(row.name || row.server_id || ''),
      extra,
    );

  const percent = { max: 100, format: (v: number) => `${Math.round(v)}%` };
  const seconds = { format: (v: number) => f.sec(v) };
  const compact = { format: (v: number) => f.compact(v) };

  const cpuUtil = lines(cpuG, 'cpu_util_pct', null, (r) => nodeName(r, word.util));
  const cpuLoad = lines(cpuG, 'load1', null, (r) => nodeName(r, word.load1), cpuUtil.length);

  const host: ChartSpec[] = [
    {
      id: 'cpu',
      title: t('openinfra.chart.cpu.title', 'CPU'),
      unit: t('openinfra.chart.cpu.unit', '% util · load (dimensionless)'),
      desc: t(
        'openinfra.chart.cpu.desc',
        'Host CPU utilization in percent and 1-minute load average from /proc. Load1 is not a percent. Each point is the mean of the per-second samples in its interval.',
      ),
      series: [...cpuUtil, ...cpuLoad],
      opts: { format: (v) => f.n(v, 1) },
    },
    {
      id: 'ram',
      title: t('openinfra.chart.ram.title', 'Host RAM'),
      unit: t('openinfra.chart.ram.unit', 'GB'),
      desc: t(
        'openinfra.chart.ram.desc',
        'Host RAM in use, in gigabytes, from (MemTotal − MemAvailable) / 1024.',
      ),
      series: lines(
        cpuG,
        'mem_used_mb',
        (v) => {
          const x = toNum(v);
          return x === null ? null : x / 1024;
        },
        (r) => nodeName(r, word.used),
      ),
      opts: { format: (v) => `${Math.round(v)}G` },
    },
  ];

  const nvTx = lines(gpuG, 'nvlink_tx_bps', toGb, (r) => gpuName(r, word.tx));
  const nvRx = lines(gpuG, 'nvlink_rx_bps', toGb, (r) => gpuName(r, word.rx), nvTx.length);
  const pciTx = lines(gpuG, 'pcie_tx_bps', toGb, (r) => gpuName(r, word.tx));
  const pciRx = lines(gpuG, 'pcie_rx_bps', toGb, (r) => gpuName(r, word.rx), pciTx.length);
  const gbps = { format: (v: number) => `${f.n(v, 1)}G` };

  const gpu: ChartSpec[] = [
    {
      id: 'gpu-sm',
      title: t('openinfra.chart.gpu_sm.title', 'GPU SM'),
      unit: '%',
      desc: t(
        'openinfra.chart.gpu_sm.desc',
        'SM (streaming multiprocessor) utilization in percent from nvidia-smi. Each point is the mean of the per-second samples in its interval.',
      ),
      series: lines(gpuG, 'utilization_pct', null, (r) => gpuName(r)),
      opts: percent,
    },
    {
      id: 'gpu-vram',
      title: t('openinfra.chart.gpu_vram.title', 'GPU VRAM'),
      unit: '%',
      desc: t(
        'openinfra.chart.gpu_vram.desc',
        'GPU VRAM used as a percent of the card’s total memory.',
      ),
      series: lines(gpuG, 'memory_pct', null, (r) => gpuName(r)),
      opts: percent,
    },
    {
      id: 'gpu-membw',
      title: t('openinfra.chart.gpu_membw.title', 'GPU memory bandwidth'),
      unit: '%',
      desc: t(
        'openinfra.chart.gpu_membw.desc',
        'Memory-controller / HBM bandwidth utilization in percent from nvidia-smi. This is not VRAM occupancy.',
      ),
      series: lines(gpuG, 'memory_util_pct', null, (r) => gpuName(r)),
      opts: percent,
    },
    {
      id: 'gpu-power',
      title: t('openinfra.chart.gpu_power.title', 'GPU power'),
      unit: 'W',
      desc: t('openinfra.chart.gpu_power.desc', 'GPU board power draw in watts.'),
      series: lines(gpuG, 'power_w', null, (r) => gpuName(r)),
      opts: { format: (v) => `${Math.round(v)}W` },
    },
    {
      id: 'gpu-temp',
      title: t('openinfra.chart.gpu_temp.title', 'GPU temperature'),
      unit: '°C',
      desc: t('openinfra.chart.gpu_temp.desc', 'GPU temperature in degrees Celsius.'),
      series: lines(gpuG, 'temperature_c', null, (r) => gpuName(r)),
      opts: { format: (v) => `${Math.round(v)}°` },
    },
    {
      id: 'gpu-clock',
      title: t('openinfra.chart.gpu_clock.title', 'GPU clocks'),
      unit: 'MHz',
      desc: t('openinfra.chart.gpu_clock.desc', 'SM clock frequency in megahertz.'),
      series: lines(gpuG, 'clocks_sm_mhz', null, (r) => gpuName(r)),
      opts: { format: (v) => `${Math.round(v)}` },
    },
    {
      id: 'dcgm-tensor',
      title: t('openinfra.chart.dcgm_tensor.title', 'DCGM tensor'),
      unit: '%',
      desc: t(
        'openinfra.chart.dcgm_tensor.desc',
        'DCGM tensor-pipe active time in percent: fraction of time Tensor Cores were busy, × 100.',
      ),
      series: lines(gpuG, 'tensor_active', times100, (r) => gpuName(r)),
      opts: percent,
    },
    {
      id: 'dcgm-dram',
      title: t('openinfra.chart.dcgm_dram.title', 'DCGM DRAM'),
      unit: '%',
      desc: t(
        'openinfra.chart.dcgm_dram.desc',
        'DCGM DRAM/HBM active time in percent: memory-pipe utilization × 100.',
      ),
      series: lines(gpuG, 'dram_active', times100, (r) => gpuName(r)),
      opts: percent,
    },
    {
      id: 'dcgm-nvlink',
      title: t('openinfra.chart.dcgm_nvlink.title', 'DCGM NVLink'),
      unit: 'GB/s',
      desc: t(
        'openinfra.chart.dcgm_nvlink.desc',
        'NVLink transmit and receive throughput in gigabytes per second.',
      ),
      series: [...nvTx, ...nvRx],
      opts: gbps,
    },
    {
      id: 'dcgm-pcie',
      title: t('openinfra.chart.dcgm_pcie.title', 'DCGM PCIe'),
      unit: 'GB/s',
      desc: t(
        'openinfra.chart.dcgm_pcie.desc',
        'PCIe transmit and receive throughput in gigabytes per second.',
      ),
      series: [...pciTx, ...pciRx],
      opts: gbps,
    },
  ];

  const running = lines(sgG, 'running', null, (r) => serverName(r, word.running));
  const queued = lines(sgG, 'queued', null, (r) => serverName(r, word.queued), running.length);
  const requestSeries = [...running, ...queued];

  const engine: ChartSpec[] = [
    {
      id: 'requests',
      title: t('openinfra.chart.requests.title', 'Requests'),
      unit: t('openinfra.chart.requests.unit', 'requests'),
      desc: t(
        'openinfra.chart.requests.desc',
        'Count of requests currently running vs waiting in the engine queue. Each point is the mean of its interval, so a brief peak is flattened.',
      ),
      series: requestSeries,
      opts: { max: Math.max(4, peak(requestSeries)), format: (v) => f.n(v) },
    },
    {
      id: 'requests-per-min',
      title: t('openinfra.chart.rpm.title', 'Requests / min'),
      unit: t('openinfra.chart.rpm.unit', 'requests/min'),
      desc: t(
        'openinfra.chart.rpm.desc',
        'Completed requests per minute from the engine request counter (or prefill log lines if the counter is missing).',
      ),
      series: lines(sgG, 'req_per_min', null, (r) => serverName(r)),
      opts: { format: (v) => f.n(v, 1) },
    },
    {
      id: 'kv-cache',
      title: t('openinfra.chart.kv.title', 'KV cache'),
      unit: '%',
      desc: t(
        'openinfra.chart.kv.desc',
        'KV cache fill in percent: 100 × used tokens / token capacity.',
      ),
      series: lines(sgG, 'token_usage', asPercent, (r) => serverName(r)),
      opts: percent,
    },
    {
      id: 'cache-hit',
      title: t('openinfra.chart.cache_hit.title', 'Cache hit'),
      unit: '%',
      desc: t(
        'openinfra.chart.cache_hit.desc',
        'Prefix-cache hit rate in percent: 100 × cached prefill tokens / (cached + new), averaged over each point’s interval. Idle intervals with no tokens are left blank.',
      ),
      series: lines(sgG, 'cache_hit_rate', asPercent, (r) => serverName(r)),
      opts: percent,
    },
    {
      id: 'tokens-per-min',
      title: t('openinfra.chart.tokens.title', 'Total tokens/min'),
      unit: t('openinfra.chart.tokens.unit', 'tokens/min'),
      desc: t(
        'openinfra.chart.tokens.desc',
        'Total tokens per minute: cached prefill + uncached prefill + decode.',
      ),
      series: lines(
        sgG,
        'prefill_tps',
        (_v, r) => perMin(prefillDecodeTps(r)),
        (r) => serverName(r),
      ),
      opts: compact,
    },
    {
      id: 'waiting-tokens',
      title: t('openinfra.chart.waiting.title', 'Waiting tokens'),
      unit: t('openinfra.chart.waiting.unit', 'tokens'),
      desc: t(
        'openinfra.chart.waiting.desc',
        'Uncached tokens waiting in the prefill queue (token count, not requests).',
      ),
      series: lines(sgG, 'waiting_tokens', null, (r) => serverName(r)),
      opts: compact,
    },
    {
      id: 'ttft',
      title: t('openinfra.chart.ttft.title', 'TTFT'),
      unit: 's',
      desc: t(
        'openinfra.chart.ttft.desc',
        'Time to first token in seconds, averaged over each point’s interval. Queue is the wait before compute, prefill is the prefill forward pass, and the total is TTFT (queue + prefill plus scheduling and tokenizing).',
      ),
      series: [
        ...lines(sgG, 'queue_avg_s', null, (r) => serverName(r, word.queue), 0, ROLE.second),
        ...lines(sgG, 'prefill_avg_s', null, (r) => serverName(r, word.prefill), 0, ROLE.first),
        ...lines(sgG, 'ttft_avg_s', null, (r) => serverName(r, word.ttft), 0, ROLE.third),
      ],
      opts: seconds,
    },
    {
      id: 'chunked-prefill',
      title: t('openinfra.chart.chunked.title', 'Chunked prefill'),
      unit: t('openinfra.chart.chunked.unit', 's / chunk'),
      desc: t(
        'openinfra.chart.chunked.desc',
        'Wall time of one prefill chunk in seconds, not the whole request. Long prompts are split so decode can run between chunks; each chunk is one sample. The full prefill is the prefill series on TTFT.',
      ),
      series: lines(sgG, 'chunked_prefill_avg_s', null, (r) => serverName(r)),
      opts: seconds,
    },
    {
      id: 'e2e',
      title: t('openinfra.chart.e2e.title', 'E2E latency'),
      unit: 's',
      desc: t(
        'openinfra.chart.e2e.desc',
        'End-to-end request latency in seconds, from arrival to completion, over requests finished in each point’s interval.',
      ),
      series: lines(sgG, 'e2e_latency_avg_s', null, (r) => serverName(r)),
      opts: seconds,
    },
    {
      id: 'itl',
      title: t('openinfra.chart.itl.title', 'ITL'),
      unit: t('openinfra.chart.itl.unit', 'ms / token'),
      desc: t(
        'openinfra.chart.itl.desc',
        'Inter-token latency after the first token, in milliseconds per output token (decode streaming speed).',
      ),
      series: lines(sgG, 'tpot_avg_s', times1000, (r) => serverName(r)),
      opts: { format: (v) => `${f.n(v, 2)} ms` },
    },
    {
      id: 'spec-length',
      title: t('openinfra.chart.spec_length.title', 'Spec accept length'),
      unit: t('openinfra.chart.spec_length.unit', 'tokens / verify'),
      desc: t(
        'openinfra.chart.spec_length.desc',
        'Speculative decoding accept length in tokens per verify step, including the bonus token.',
      ),
      series: lines(sgG, 'spec_accept_length', null, (r) => serverName(r)),
      opts: { format: (v) => f.n(v, 2) },
    },
    {
      id: 'spec-rate',
      title: t('openinfra.chart.spec_rate.title', 'Spec accept rate'),
      unit: '%',
      desc: t(
        'openinfra.chart.spec_rate.desc',
        'Speculative accept rate in percent: 100 × accepted draft tokens / proposed draft tokens.',
      ),
      series: lines(sgG, 'spec_accept_rate', asPercent, (r) => serverName(r)),
      opts: percent,
    },
    {
      id: 'hicache-host',
      title: t('openinfra.chart.hicache.title', 'HiCache host'),
      unit: '%',
      desc: t(
        'openinfra.chart.hicache.desc',
        'Host (CPU DRAM) hierarchical KV cache fill in percent. 100% means the host cache is full and further GPU evictions may drop or overwrite.',
      ),
      series: lines(
        sgG,
        'hicache_host_used',
        (_v, r) => {
          const used = toNum(r.hicache_host_used);
          const total = toNum(r.hicache_host_total);
          return used === null || total === null || total <= 0 ? null : (100 * used) / total;
        },
        (r) => serverName(r),
      ),
      opts: percent,
    },
    {
      id: 'cache-source',
      title: t('openinfra.chart.cache_source.title', 'Cache source'),
      unit: t('openinfra.chart.cache_source.unit', 'tokens/min'),
      desc: t(
        'openinfra.chart.cache_source.desc',
        'Cached prompt tokens per minute by source: the GPU radix cache vs the host cache.',
      ),
      series: [
        ...lines(
          sgG,
          'device_cached_tps',
          (_v, r) => perMin(r.device_cached_tps),
          (r) => serverName(r, word.device),
          0,
          ROLE.first,
        ),
        ...lines(
          sgG,
          'host_cached_tps',
          (_v, r) => perMin(r.host_cached_tps),
          (r) => serverName(r, word.host),
          0,
          ROLE.second,
        ),
      ],
      opts: compact,
    },
    {
      id: 'hicache-traffic',
      title: t('openinfra.chart.hicache_traffic.title', 'HiCache traffic'),
      unit: t('openinfra.chart.hicache_traffic.unit', 'tokens/min'),
      desc: t(
        'openinfra.chart.hicache_traffic.desc',
        'HiCache movement in tokens per minute: backup GPU→host, load-back host→GPU, and device KV slots freed by eviction.',
      ),
      series: [
        ...lines(
          sgG,
          'backup_tps',
          (_v, r) => perMin(r.backup_tps),
          (r) => serverName(r, word.backup),
          0,
          ROLE.first,
        ),
        ...lines(
          sgG,
          'loadback_tps',
          (_v, r) => perMin(r.loadback_tps),
          (r) => serverName(r, word.loadback),
          0,
          ROLE.second,
        ),
        ...lines(
          sgG,
          'evict_tps',
          (_v, r) => perMin(r.evict_tps),
          (r) => serverName(r, word.evict),
          0,
          ROLE.third,
        ),
      ],
      opts: compact,
    },
    {
      id: 'request-shape',
      title: t('openinfra.chart.shape.title', 'Request shape'),
      unit: t('openinfra.chart.shape.unit', 'tokens / request'),
      desc: t(
        'openinfra.chart.shape.desc',
        'Mean token counts per request in each point’s interval: the full prompt, the uncached (computed) prompt, and the generation length.',
      ),
      series: [
        ...lines(sgG, 'prompt_tokens_avg', null, (r) => serverName(r, word.prompt), 0, ROLE.first),
        ...lines(
          sgG,
          'uncached_prompt_tokens_avg',
          null,
          (r) => serverName(r, word.uncached),
          0,
          ROLE.second,
        ),
        ...lines(sgG, 'generation_tokens_avg', null, (r) => serverName(r, word.gen), 0, ROLE.third),
      ],
      opts: compact,
    },
    {
      id: 'http-errors',
      title: t('openinfra.chart.http_errors.title', 'HTTP errors'),
      unit: t('openinfra.chart.http_errors.unit', 'responses/min'),
      desc: t(
        'openinfra.chart.http_errors.desc',
        'HTTP 4xx and 5xx responses per minute from the engine’s metrics counters.',
      ),
      series: lines(sgG, 'http_error_per_min', null, (r) => serverName(r)),
      opts: { format: (v) => f.n(v, 2) },
    },
    {
      id: 'retracts',
      title: t('openinfra.chart.retracts.title', 'KV retracts'),
      unit: t('openinfra.chart.retracts.unit', 'requests'),
      desc: t(
        'openinfra.chart.retracts.desc',
        'Count of requests currently retracted because the KV cache ran out of space. A spike means cache pressure.',
      ),
      series: lines(sgG, 'retracts', null, (r) => serverName(r)),
      opts: { format: (v) => f.n(v, 1) },
    },
  ];

  return { host, gpu, engine };
}
