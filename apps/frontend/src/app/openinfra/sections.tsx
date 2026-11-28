'use client';

import type { ReactNode } from 'react';

import type {
  OpenInfraEngine,
  OpenInfraGpu,
  OpenInfraStatus,
  ServedBucket,
} from '@/lib/api/openinfra';
import { fill } from '@/lib/utils/interpolate';
import { ROLE } from './charts';
import { NONE, type InfraFormat } from './format';
import { perMin, prefillDecodeTps, servedTotals, shortModel, type DashboardView } from './model';

/** Green under 40%, amber under 75%, red from there: a utilization bar's color. */
export function heat(util: number | null | undefined): string {
  const u = Math.max(0, Math.min(100, util ?? 0));
  if (u < 40) return '#1baf7a';
  if (u < 75) return '#c99100';
  return '#d6336c';
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={`min-w-0 rounded-2xl border border-gray-200 bg-white p-4 shadow-sm sm:p-5 ${className}`}
    >
      {children}
    </div>
  );
}

export function SectionHeading({
  id,
  title,
  subtitle,
  children,
}: {
  id: string;
  title: string;
  subtitle: string;
  children?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-2">
      <div>
        <h2 id={id} className="font-serif text-2xl font-semibold text-gray-900">
          {title}
        </h2>
        <p className="text-sm text-gray-500">{subtitle}</p>
      </div>
      {children}
    </header>
  );
}

export interface Tile {
  key: string;
  label: string;
  value: string;
  hint: string;
}

export function KpiTiles({ tiles, label }: { tiles: Tile[]; label: string }) {
  return (
    <div
      role="group"
      aria-label={label}
      className={`grid gap-3 ${tiles.length > 2 ? 'grid-cols-2 lg:grid-cols-4' : 'grid-cols-2'}`}
    >
      {tiles.map((tile) => (
        <Card key={tile.key}>
          <div className="text-xs font-medium uppercase tracking-wide text-gray-500">
            {tile.label}
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums text-gray-950">{tile.value}</div>
          <div className="mt-1 text-xs text-gray-500">{tile.hint}</div>
        </Card>
      ))}
    </div>
  );
}

export function statusLabel(status: OpenInfraStatus, f: InfraFormat): string {
  const { t } = f;
  switch (status) {
    case 'online':
      return t('openinfra.status.online', 'online');
    case 'degraded':
      return t('openinfra.status.degraded', 'degraded');
    case 'offline':
      return t('openinfra.status.offline', 'offline');
    default:
      return t('openinfra.status.unknown', 'unknown');
  }
}

function ServedCard({
  title,
  windowLabel,
  totals,
  f,
}: {
  title: string;
  windowLabel: string;
  totals: ServedBucket;
  f: InfraFormat;
}) {
  const { t } = f;
  const cache = totals.cache_tokens;
  const prefill = totals.prefill_tokens;
  const decode = totals.decode_tokens;
  const tokens = cache + prefill + decode;
  const hitDen = cache + prefill;
  const share = (value: number) => (tokens > 0 ? f.pct((100 * value) / tokens) : NONE);
  const width = (value: number) => (tokens > 0 ? Math.max(0, (100 * value) / tokens) : 0);
  const parts = [
    { key: 'cache', label: t('openinfra.served.cache', 'cache'), value: cache, color: ROLE.first },
    {
      key: 'prefill',
      label: t('openinfra.served.prefill', 'prefill'),
      value: prefill,
      color: ROLE.second,
    },
    {
      key: 'decode',
      label: t('openinfra.served.decode', 'decode'),
      value: decode,
      color: ROLE.third,
    },
  ];
  return (
    <Card>
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="font-semibold text-gray-900">{title}</h3>
        <span className="text-xs text-gray-500">{windowLabel}</span>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <div className="text-3xl font-semibold tabular-nums text-gray-950">
            {f.compact(totals.reqs)}
          </div>
          <div className="text-xs text-gray-500">{t('openinfra.served.requests', 'requests')}</div>
        </div>
        <div>
          <div className="text-3xl font-semibold tabular-nums text-gray-950">
            {f.compact(tokens)}
          </div>
          <div className="text-xs text-gray-500">{t('openinfra.served.tokens', 'tokens')}</div>
        </div>
      </div>
      <div
        role="img"
        aria-label={fill(
          t('openinfra.served.mix', 'Token mix: cache {cache}, prefill {prefill}, decode {decode}'),
          { cache: share(cache), prefill: share(prefill), decode: share(decode) },
        )}
        className="mt-4 flex h-2.5 overflow-hidden rounded-full bg-gray-100"
      >
        {parts.map((part) => (
          <span
            key={part.key}
            className="h-full"
            style={{ width: `${width(part.value)}%`, backgroundColor: part.color }}
          />
        ))}
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-sm">
        {parts.map((part) => (
          <div key={part.key} className="min-w-0">
            <div className="flex items-center gap-1.5 text-xs text-gray-500">
              <span
                aria-hidden="true"
                className="h-2 w-2 shrink-0 rounded-sm"
                style={{ backgroundColor: part.color }}
              />
              {part.label}
            </div>
            <div className="font-semibold tabular-nums text-gray-900">{f.compact(part.value)}</div>
            <div className="text-xs tabular-nums text-gray-500">{share(part.value)}</div>
          </div>
        ))}
      </div>
      <p className="mt-3 text-xs text-gray-600">
        {hitDen > 0
          ? fill(t('openinfra.served.hit', 'Window cache hit {percent}'), {
              percent: f.pct((100 * cache) / hitDen),
            })
          : t('openinfra.served.no_prefill', 'No prefill tokens in this window')}
      </p>
    </Card>
  );
}

/** Requests and tokens the deployment shown delivered in the last hour and the last day. */
export function ServedSection({ view, f }: { view: DashboardView; f: InfraFormat }) {
  const { t } = f;
  const { hour, day } = servedTotals(view);
  return (
    <section className="space-y-3" aria-labelledby="openinfra-served">
      <SectionHeading
        id="openinfra-served"
        title={t('openinfra.served.title', 'Served')}
        subtitle={t(
          'openinfra.served.subtitle',
          'Requests and tokens delivered · last hour and last 24 hours',
        )}
      />
      <div className="grid gap-3 md:grid-cols-2">
        <ServedCard
          title={t('openinfra.served.hour', 'Last hour')}
          windowLabel={t('openinfra.served.hour_window', '60 min')}
          totals={hour}
          f={f}
        />
        <ServedCard
          title={t('openinfra.served.day', 'Last 24 hours')}
          windowLabel={t('openinfra.served.day_window', '1 day')}
          totals={day}
          f={f}
        />
      </div>
    </section>
  );
}

function GpuCard({ gpu, f }: { gpu: OpenInfraGpu; f: InfraFormat }) {
  const { t } = f;
  const u = gpu.utilization_pct ?? 0;
  const memPct = gpu.memory_total_mb ? (100 * (gpu.memory_used_mb || 0)) / gpu.memory_total_mb : 0;
  const ratio = (value: number | null) => (value === null ? NONE : f.pct(value * 100));
  const stats: Array<[string, string]> = [
    [t('openinfra.gpu_card.util', 'util'), f.pct(u)],
    [t('openinfra.gpu_card.vram', 'VRAM'), f.pct(memPct)],
    [t('openinfra.gpu_card.temp', 'temp'), `${f.n(gpu.temperature_c, 1)}°C`],
    [t('openinfra.gpu_card.power', 'power'), `${f.n(gpu.power_w)}W`],
    [t('openinfra.gpu_card.mem_engine', 'mem eng'), f.pct(gpu.memory_util_pct)],
    [t('openinfra.gpu_card.sm_clock', 'SM clk'), `${f.n(gpu.clocks_sm_mhz)}MHz`],
    [t('openinfra.gpu_card.tensor', 'tensor'), ratio(gpu.tensor_active)],
    [t('openinfra.gpu_card.dram', 'DRAM'), ratio(gpu.dram_active)],
  ];
  return (
    <div className="rounded-xl border border-gray-200 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-sm font-medium text-gray-900">
          {gpu.name ? gpu.name.replace('NVIDIA ', '') : 'GPU'}
        </span>
        <span className="font-mono text-xs text-gray-500">#{gpu.gpu_index}</span>
      </div>
      <div
        className="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-100"
        title={fill(t('openinfra.gpu_card.bar', 'Utilization {percent}'), { percent: f.pct(u) })}
      >
        <span
          className="block h-full rounded-full"
          style={{ width: `${Math.max(0, Math.min(100, u))}%`, backgroundColor: heat(u) }}
        />
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs">
        {stats.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-2">
            <dt className="text-gray-500">{label}</dt>
            <dd className="font-medium tabular-nums text-gray-900">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** Each node of the view, with its GPUs. */
export function GpuFleet({ view, f }: { view: DashboardView; f: InfraFormat }) {
  const { t } = f;
  const byNode = new Map<string, OpenInfraGpu[]>();
  for (const gpu of view.gpus) {
    const list = byNode.get(gpu.node_id);
    if (list) list.push(gpu);
    else byNode.set(gpu.node_id, [gpu]);
  }
  return (
    <Card>
      <h3 className="font-semibold text-gray-900">{t('openinfra.fleet.title', 'GPU fleet')}</h3>
      {view.nodes.length === 0 ? (
        <p className="mt-2 text-sm text-gray-500">
          {t('openinfra.fleet.empty', 'No node has reported yet.')}
        </p>
      ) : (
        <div className="mt-3 space-y-4">
          {view.nodes.map((node) => {
            const gpus = byNode.get(node.id) ?? [];
            return (
              <div key={node.id}>
                <h4 className="text-sm font-semibold text-gray-900">
                  {node.hostname}{' '}
                  <span className="font-normal text-gray-500">
                    {fill(
                      t(
                        'openinfra.fleet.node_meta',
                        '{region} · {gpus} GPU · {util} · CPU {cpu} · load {load} · RAM {used}/{total} GB',
                      ),
                      {
                        region: node.region || NONE,
                        gpus: String(gpus.length),
                        util: f.pct(node.avg_utilization_pct),
                        cpu: f.pct(node.cpu_util_pct),
                        load: f.n(node.load1, 2),
                        used: f.gb(node.mem_used_mb),
                        total: f.gb(node.mem_total_mb),
                      },
                    )}
                  </span>
                </h4>
                <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                  {gpus.map((gpu) => (
                    <GpuCard key={gpu.id} gpu={gpu} f={f} />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function engineRows(s: OpenInfraEngine, f: InfraFormat): Array<[string, string]> {
  const { t } = f;
  const opt = (value: number | null, write: (v: number) => string) =>
    value === null || value === undefined ? NONE : write(value);
  const perMinute = (value: number | null) => opt(value, (v) => f.compact(perMin(v)));
  const pair = (a: number | null, b: number | null) =>
    a === null && b === null ? NONE : `${f.compact(a)} / ${f.compact(b)}`;
  const kvFill = s.token_usage !== null ? s.token_usage * 100 : null;
  const rows: Array<[string, string]> = [
    [t('openinfra.engine_card.running', 'running'), f.n(s.num_running_reqs || 0)],
    [t('openinfra.engine_card.queued', 'queued'), f.n(s.num_queue_reqs || 0)],
    [t('openinfra.engine_card.rpm', 'req/min'), opt(s.req_per_min, (v) => f.n(v, 1))],
    [t('openinfra.engine_card.kv_fill', 'KV fill'), opt(kvFill, (v) => f.pct(v))],
    [
      t('openinfra.engine_card.cache_hit', 'cache hit'),
      opt(s.cache_hit_rate, (v) => f.pct(v * 100)),
    ],
    [
      t('openinfra.engine_card.kv_tokens', 'KV tokens'),
      pair(s.num_used_tokens, s.max_total_num_tokens),
    ],
    [t('openinfra.engine_card.prefill_min', 'prefill/min'), perMinute(s.prefill_tps)],
    [t('openinfra.engine_card.decode_min', 'decode/min'), perMinute(s.gen_throughput)],
    [
      t('openinfra.engine_card.cached_prefill_min', 'cached prefill/min'),
      perMinute(s.cached_prefill_tps),
    ],
    [t('openinfra.engine_card.total_min', 'total tok/min'), perMinute(prefillDecodeTps(s))],
    [t('openinfra.engine_card.ttft', 'TTFT'), opt(s.ttft_avg_s, f.sec)],
    [t('openinfra.engine_card.queue', 'queue'), opt(s.queue_avg_s, f.sec)],
    [t('openinfra.engine_card.prefill', 'prefill'), opt(s.prefill_avg_s, f.sec)],
    [t('openinfra.engine_card.chunked', 'chunked prefill'), opt(s.chunked_prefill_avg_s, f.sec)],
    [t('openinfra.engine_card.e2e', 'e2e'), opt(s.e2e_latency_avg_s, f.sec)],
    [t('openinfra.engine_card.itl', 'ITL'), opt(s.tpot_avg_s, f.ms)],
    [
      t('openinfra.engine_card.spec_accept', 'spec accept'),
      opt(s.spec_accept_length, (v) => f.n(v, 2)),
    ],
    [
      t('openinfra.engine_card.spec_rate', 'spec rate'),
      opt(s.spec_accept_rate, (v) => f.pct(v * 100)),
    ],
    [
      t('openinfra.engine_card.hicache', 'HiCache'),
      s.hicache_host_used === null || s.hicache_host_total === null
        ? NONE
        : pair(s.hicache_host_used, s.hicache_host_total),
    ],
    [t('openinfra.engine_card.device_cache', 'device cache/min'), perMinute(s.device_cached_tps)],
    [t('openinfra.engine_card.host_cache', 'host cache/min'), perMinute(s.host_cached_tps)],
    [t('openinfra.engine_card.backup', 'backup/min'), perMinute(s.backup_tps)],
    [t('openinfra.engine_card.loadback', 'load-back/min'), perMinute(s.loadback_tps)],
    [t('openinfra.engine_card.evict', 'evict/min'), perMinute(s.evict_tps)],
    [
      t('openinfra.engine_card.prompt_tokens', 'prompt toks'),
      opt(s.prompt_tokens_avg, (v) => f.compact(v)),
    ],
    [
      t('openinfra.engine_card.uncached_prompt', 'uncached prompt'),
      opt(s.uncached_prompt_tokens_avg, (v) => f.compact(v)),
    ],
    [
      t('openinfra.engine_card.generation_tokens', 'gen toks'),
      opt(s.generation_tokens_avg, (v) => f.compact(v)),
    ],
    [
      t('openinfra.engine_card.http_errors', 'HTTP err/min'),
      opt(s.http_error_per_min, (v) => f.n(v, 2)),
    ],
    [t('openinfra.engine_card.retracts', 'retracts'), opt(s.retracts, (v) => f.n(v, 1))],
    [
      t('openinfra.engine_card.engine_util', 'engine util'),
      opt(s.sglang_util_pct, (v) => f.pct(v)),
    ],
  ];
  return rows.filter(([, value]) => value !== NONE);
}

function EngineCard({ s, f, wide }: { s: OpenInfraEngine; f: InfraFormat; wide: boolean }) {
  const { t } = f;
  const run = s.num_running_reqs || 0;
  const wait = s.num_queue_reqs || 0;
  const kvFill = s.token_usage !== null ? s.token_usage * 100 : null;
  const bar = kvFill ?? s.sglang_util_pct ?? Math.min(100, run + wait);
  const model =
    shortModel(s.model_name) ?? t('openinfra.engine_card.unknown_model', 'unknown model');
  const gpus = s.gpu_indexes
    ? fill(t('openinfra.engine_card.gpus', 'GPU {indexes}'), { indexes: s.gpu_indexes })
    : '';
  return (
    <div className="rounded-xl border border-gray-200 p-4">
      <h4 className="flex flex-wrap items-center gap-2 font-semibold text-gray-900">
        <span className="min-w-0 break-words">{s.name || s.id}</span>
        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
          {s.engine || 'sglang'}
        </span>
      </h4>
      <div className="mt-0.5 break-words text-sm text-gray-500">
        {gpus ? `${model} · ${gpus}` : model}
      </div>
      <div className="mt-3 flex items-end justify-between gap-2">
        <div>
          <div className="text-3xl font-semibold tabular-nums text-gray-950">{f.n(wait)}</div>
          <div className="text-xs text-gray-500">
            {t('openinfra.engine_card.in_queue', 'in queue')}
          </div>
        </div>
        <div className="text-right text-xs text-gray-500">
          {[s.hostname || s.node_id || '', statusLabel(s.status, f)].filter(Boolean).join(' · ')}
        </div>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-100">
        <span
          className="block h-full rounded-full"
          style={{ width: `${Math.max(0, Math.min(100, bar))}%`, backgroundColor: heat(bar) }}
        />
      </div>
      <dl
        className={`mt-3 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs sm:grid-cols-3 ${
          wide ? 'lg:grid-cols-4 lg:gap-x-8 2xl:grid-cols-5' : ''
        }`}
      >
        {engineRows(s, f).map(([label, value]) => (
          <div key={label} className="flex justify-between gap-2">
            <dt className="text-gray-500">{label}</dt>
            <dd className="font-medium tabular-nums text-gray-900">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** The engine(s) of the view, each with every figure it reports. */
export function EngineCards({ view, f }: { view: DashboardView; f: InfraFormat }) {
  const { t } = f;
  return (
    <Card>
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="font-semibold text-gray-900">{t('openinfra.deploy.title', 'Deployment')}</h3>
        <span className="text-xs text-gray-500">
          {fill(t('openinfra.deploy.waiting', '{count} waiting'), {
            count: f.n(view.kpis.queue_reqs || 0),
          })}
        </span>
      </div>
      {view.sglang.length === 0 ? (
        <p className="mt-2 text-sm text-gray-500">
          {t('openinfra.deploy.empty', 'No engine has reported yet.')}
        </p>
      ) : (
        <div className={`mt-3 grid gap-3 ${view.sglang.length > 1 ? 'xl:grid-cols-2' : ''}`}>
          {view.sglang.map((s) => (
            <EngineCard key={s.id} s={s} f={f} wide={view.sglang.length === 1} />
          ))}
        </div>
      )}
    </Card>
  );
}

/** The latest requests the view's engine logged: their shape and latency, nothing they said. */
export function RecentRequests({ view, f }: { view: DashboardView; f: InfraFormat }) {
  const { t } = f;
  const rows = [...view.requests].sort((a, b) => (b.ts || 0) - (a.ts || 0));
  const cell = (v: number | null, digits?: 0 | 1 | 2) =>
    v === null || Number.isNaN(v) ? NONE : digits === undefined ? f.compact(v) : f.n(v, digits);
  const head = (label: string, left = false, title?: string) => (
    <th
      scope="col"
      title={title}
      className={`whitespace-nowrap px-2 py-1.5 font-medium text-gray-500 ${left ? 'text-left' : 'text-right'}`}
    >
      {label}
    </th>
  );
  return (
    <Card>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-semibold text-gray-900">
          {t('openinfra.requests.title', 'Recent requests')}
        </h3>
        <span className="text-xs text-gray-500">
          {rows.length
            ? fill(t('openinfra.requests.count', '{count} anonymized · no prompts'), {
                count: String(rows.length),
              })
            : t('openinfra.requests.anonymized', 'anonymized · no prompts')}
        </span>
      </div>
      <p className="mt-1 text-xs text-gray-500">
        {t(
          'openinfra.requests.help',
          'Per-request shape and latency from engine logs. No prompts, completions, IP addresses or request ids. Out is generated tokens until the request leaves the decode batch (from generation throughput × time / running).',
        )}
      </p>
      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-gray-500">
          {t(
            'openinfra.requests.empty',
            'No per-request metadata yet for this deployment. Rows appear after the next reporter flush from engine logs.',
          )}
        </p>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[56rem] text-xs tabular-nums">
            <thead className="border-b border-gray-200">
              <tr>
                {head(t('openinfra.requests.col_time', 'time'), true)}
                {head(t('openinfra.requests.col_model', 'model'), true)}
                {head(t('openinfra.requests.col_in', 'in'))}
                {head(t('openinfra.requests.col_cache', 'cache'))}
                {head(t('openinfra.requests.col_uncached', 'uncached'))}
                {head(
                  t('openinfra.requests.col_out', 'out'),
                  false,
                  t(
                    'openinfra.requests.col_out_title',
                    'Generated tokens until the request leaves the decode batch, from generation throughput × time / running',
                  ),
                )}
                {head(t('openinfra.requests.col_ttft', 'TTFT'))}
                {head(t('openinfra.requests.col_prefill', 'prefill'))}
                {head(t('openinfra.requests.col_tpot', 'TPOT'))}
                {head(t('openinfra.requests.col_e2e', 'e2e'))}
                {head(t('openinfra.requests.col_decode', 'decode'))}
                {head(t('openinfra.requests.col_spec', 'spec'))}
                {head(t('openinfra.requests.col_batch', 'batch'))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {rows.map((r, i) => {
                const spec =
                  r.spec_accept_len === null && r.spec_accept_rate === null
                    ? NONE
                    : `${r.spec_accept_len === null ? NONE : f.n(r.spec_accept_len, 2)}${
                        r.spec_accept_rate === null ? '' : ` · ${f.pct(r.spec_accept_rate * 100)}`
                      }`;
                return (
                  <tr key={`${r.server_id}:${r.ts}:${i}`} className="text-gray-800">
                    <td className="whitespace-nowrap px-2 py-1 text-left font-mono">
                      {f.clock(r.ts)}
                    </td>
                    <td className="max-w-[14rem] truncate px-2 py-1 text-left">
                      {shortModel(r.model) ?? NONE}
                    </td>
                    <td className="px-2 py-1 text-right">{cell(r.input_len)}</td>
                    <td className="px-2 py-1 text-right">{cell(r.cache_len)}</td>
                    <td className="px-2 py-1 text-right">{cell(r.uncached_len)}</td>
                    <td className="px-2 py-1 text-right">{cell(r.output_len)}</td>
                    <td className="px-2 py-1 text-right">{f.sec(r.ttft_s)}</td>
                    <td className="px-2 py-1 text-right">{f.sec(r.prefill_s)}</td>
                    <td className="px-2 py-1 text-right">{f.sec(r.tpot_s)}</td>
                    <td className="px-2 py-1 text-right">{f.sec(r.e2e_s)}</td>
                    <td className="px-2 py-1 text-right">
                      {r.decode_tps === null ? NONE : f.compact(r.decode_tps)}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1 text-right">{spec}</td>
                    <td className="px-2 py-1 text-right">{cell(r.batch_size, 0)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
