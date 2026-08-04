'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AdminMetricDistribution,
  AdminPerformanceMetricsWindow,
  ProviderTokenUsageResponse,
  ProviderTokenUsageRow,
  TokenUsageRange,
  getProviderTokenUsage,
  getPerformanceMetrics,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const RANGES: { key: TokenUsageRange; label: string }[] = [
  { key: '1h', label: 'Last 1h' },
  { key: '24h', label: 'Last 24h' },
  { key: '7d', label: 'Last 7d' },
  { key: '30d', label: 'Last 30d' },
];

const compact = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

function fmtCount(n: number): string {
  return n >= 10_000 ? compact.format(n) : n.toLocaleString();
}

function fmtCost(usd: number): string {
  if (usd === 0) return '$0';
  if (Math.abs(usd) < 1) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

function fmtUtcHour(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getUTCHours()).padStart(2, '0')}:00 UTC`;
}

function fmtUtcRange(fromIso: string, toIso: string): string {
  const from = new Date(fromIso);
  const to = new Date(toIso);
  const fmt = (d: Date) =>
    `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
  return `${fmt(from)}–${fmt(to)} UTC`;
}

function rowTotal(r: ProviderTokenUsageRow): number {
  return r.input_tokens + r.output_tokens + r.cached_tokens + r.reasoning_tokens;
}

function groupByProvider(
  rows: ProviderTokenUsageRow[],
): { provider: string; label: string; rows: ProviderTokenUsageRow[]; total: number }[] {
  const buckets = new Map<string, ProviderTokenUsageRow[]>();
  for (const r of rows) {
    const arr = buckets.get(r.provider) ?? [];
    arr.push(r);
    buckets.set(r.provider, arr);
  }
  const out: { provider: string; label: string; rows: ProviderTokenUsageRow[]; total: number }[] =
    [];
  for (const [provider, providerRows] of buckets) {
    const total = providerRows.reduce((acc, r) => acc + rowTotal(r), 0);
    // A relabelled route (models.yaml `provider_display_name:`) shows its name
    // with the raw label kept alongside, since that label is what api_logs and
    // the other provider tabs are keyed on.
    const displayName = providerRows.find((r) => r.provider_display_name)?.provider_display_name;
    const label =
      displayName && displayName !== provider ? `${displayName} · ${provider}` : provider;
    out.push({ provider, label, rows: providerRows, total });
  }
  out.sort((a, b) => b.total - a.total);
  return out;
}

export function TokenUsageTab({ perfRefreshNonce }: { perfRefreshNonce?: number }) {
  const [range, setRange] = useState<TokenUsageRange>('24h');
  const [data, setData] = useState<ProviderTokenUsageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await getProviderTokenUsage(range);
      setData(resp);
    } catch (exc) {
      setError(getErrorMessage(exc));
    } finally {
      setLoading(false);
    }
  }, [range]);

  useEffect(() => {
    void load();
  }, [load, perfRefreshNonce]);

  const groups = useMemo(() => groupByProvider(data?.rows ?? []), [data]);

  return (
    <div className="space-y-6 mt-6">
      <div className="flex flex-wrap items-end gap-4">
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Range</span>
          <select
            className="border rounded px-2 py-1"
            value={range}
            onChange={(e) => setRange(e.target.value as TokenUsageRange)}
          >
            {RANGES.map((r) => (
              <option key={r.key} value={r.key}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        {data ? (
          <div className="text-[12px] text-gray-500 pb-1">
            <div>Updated at {fmtUtcHour(data.refreshed_at)} (hourly refresh)</div>
            {range === '1h' ? (
              <div>showing hour {fmtUtcRange(data.window.from, data.window.to)}</div>
            ) : null}
          </div>
        ) : null}
      </div>

      {error ? <div className="text-red-600 text-sm">{error}</div> : null}
      {loading ? <div className="text-gray-500 text-sm">Loading…</div> : null}

      {data ? (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            <Kpi label="Input" value={fmtCount(data.totals.input_tokens)} />
            <Kpi label="Output" value={fmtCount(data.totals.output_tokens)} />
            <Kpi label="Cached" value={fmtCount(data.totals.cached_tokens)} />
            <Kpi label="Reasoning" value={fmtCount(data.totals.reasoning_tokens)} />
            <Kpi label="Requests" value={fmtCount(data.totals.request_count)} />
            <Kpi label="Cost USD" value={fmtCost(data.totals.cost_usd)} />
          </div>

          {groups.length === 0 ? (
            <div className="rounded-xl border p-6 text-center text-[13px] text-gray-400">
              No token usage recorded in this window.
            </div>
          ) : (
            <div className="space-y-4">
              {groups.map((g) => (
                <ProviderTable key={g.provider} provider={g.label} rows={g.rows} />
              ))}
            </div>
          )}
        </>
      ) : null}

      <PerformanceMetricsSection refreshKey={perfRefreshNonce} />
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border p-3">
      <p className="text-[11px] uppercase tracking-wide text-gray-400">{label}</p>
      <p className="mt-1 text-lg font-bold text-gray-900 tabular-nums">{value}</p>
    </div>
  );
}

function ProviderTable({ provider, rows }: { provider: string; rows: ProviderTokenUsageRow[] }) {
  return (
    <div className="rounded-xl border overflow-hidden">
      <div className="bg-gray-50 px-4 py-2 text-[13px] font-semibold text-gray-900">{provider}</div>
      <table className="w-full text-[12px]">
        <thead className="text-gray-500">
          <tr className="border-t">
            <th className="text-left px-4 py-2">Model</th>
            <th className="text-right px-3 py-2">Input</th>
            <th className="text-right px-3 py-2">Output</th>
            <th className="text-right px-3 py-2">Cached</th>
            <th className="text-right px-3 py-2">Reasoning</th>
            <th className="text-right px-3 py-2">Requests</th>
            <th className="text-right px-4 py-2">Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.model_id} className="border-t">
              <td className="px-4 py-2 text-gray-900">{r.model_id}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.input_tokens)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.output_tokens)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.cached_tokens)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.reasoning_tokens)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.request_count)}</td>
              <td className="px-4 py-2 text-right tabular-nums">{fmtCost(r.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const FORMAT_FUNCTIONS: Record<string, (v: number) => string> = {
  tokens: (v: number) =>
    v >= 1000 ? `${(v / 1000).toFixed(v % 1000 === 0 ? 0 : 1)}k` : v.toLocaleString(),
  ms: (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(v % 1000 === 0 ? 0 : 1)}s` : `${v}ms`),
  tps: (v: number) =>
    v >= 1000 ? `${(v / 1000).toFixed(v % 1000 === 0 ? 0 : 1)}k tok/s` : `${v.toFixed(1)} tok/s`,
};

function formatValue(v: number | null | undefined, kind: string): string {
  if (v == null) return '—';
  const fn = FORMAT_FUNCTIONS[kind] ?? FORMAT_FUNCTIONS.ms;
  return fn(v);
}

function PerformanceMetricsCard({ metric }: { metric: AdminPerformanceMetricsWindow }) {
  const rows: Array<{ title: string; dist: AdminMetricDistribution; kind: string }> = [
    { title: 'Prompt tokens', dist: metric.prompt_tokens, kind: 'tokens' },
    { title: 'Response tokens', dist: metric.completion_tokens, kind: 'tokens' },
    { title: 'TTFT', dist: metric.ttft_ms, kind: 'ms' },
    { title: 'Throughput', dist: metric.throughput_tps, kind: 'tps' },
  ];
  return (
    <div className="rounded-xl border border-gray-200 bg-white px-3 py-2.5 shadow-sm">
      <div className="flex items-center justify-between">
        <div className="text-[12px] font-semibold text-gray-900">{metric.label}</div>
        <div className="text-[10px] text-gray-400">{metric.window_minutes}m window</div>
      </div>
      <table className="mt-2 w-full">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-gray-400 font-medium">
            <th className="py-1 text-left">Metric</th>
            <th className="py-1 text-right">n</th>
            <th className="py-1 text-right">p50</th>
            <th className="py-1 text-right">p95</th>
            <th className="py-1 text-right">p99</th>
          </tr>
        </thead>
        <tbody className="[&>tr+tr>td]:border-t [&>tr+tr>td]:border-gray-100">
          {rows.map((row) => (
            <tr key={row.title}>
              <td className="py-1.5 text-[11px] text-gray-600">{row.title}</td>
              <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                {row.dist.count.toLocaleString()}
              </td>
              <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                {formatValue(row.dist.p50, row.kind)}
              </td>
              <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                {formatValue(row.dist.p95, row.kind)}
              </td>
              <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                {formatValue(row.dist.p99, row.kind)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PerformanceMetricsSection({ refreshKey }: { refreshKey?: number }) {
  const [perfMetrics, setPerfMetrics] = useState<AdminPerformanceMetricsWindow[]>([]);
  const [perfMetricsLoading, setPerfMetricsLoading] = useState(false);

  const loadPerformanceMetrics = useCallback(async () => {
    setPerfMetricsLoading(true);
    try {
      const d = await getPerformanceMetrics();
      setPerfMetrics(d.windows);
    } catch {
      // ignore
    } finally {
      setPerfMetricsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPerformanceMetrics();
  }, [loadPerformanceMetrics, refreshKey]);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <div>
          <h2 className="text-[14px] font-semibold text-gray-900">Performance metrics</h2>
          <p className="text-[11px] text-gray-400">
            Prompt/response length, time-to-first-token, and inter-token latency distributions.
          </p>
        </div>
        {perfMetricsLoading && (
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        )}
      </div>
      {perfMetrics.length > 0 ? (
        <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-2">
          {perfMetrics.map((metric) => (
            <PerformanceMetricsCard key={metric.key} metric={metric} />
          ))}
        </div>
      ) : !perfMetricsLoading ? (
        <div className="rounded-xl border border-dashed border-gray-200 py-8 text-center">
          <p className="text-[13px] text-gray-400">No performance metrics available.</p>
        </div>
      ) : null}
    </div>
  );
}
