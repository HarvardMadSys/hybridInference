'use client';

import { useEffect, useMemo, useState } from 'react';
import {
  AdminMetricDistribution,
  AdminPerformanceMetricsWindow,
  ProviderTokenUsageResponse,
  ProviderTokenUsageRow,
  TokenUsageRange,
  getProviderTokenUsage,
  getPerformanceMetrics,
} from '@/lib/api/admin';
import { ComputedAt } from '@/components/features/admin/ComputedAt';
import { useRefreshedAdminView } from '@/components/features/admin/useRefreshedAdminView';
import { getErrorMessage } from '@/lib/utils/errors';

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

/** Token totals per provider and model; the Analytics overview picks the range. */
export function TokenUsageSection({ range }: { range: TokenUsageRange }) {
  const [data, setData] = useState<ProviderTokenUsageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The overview's period buttons can be clicked faster than the server
  // answers: a superseded range must not land its rows, or clear `loading`,
  // over the one now selected.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const resp = await getProviderTokenUsage(range);
        if (!cancelled) setData(resp);
      } catch (exc) {
        if (!cancelled) setError(getErrorMessage(exc));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [range]);

  const groups = useMemo(() => groupByProvider(data?.rows ?? []), [data]);
  const shown = !loading && !error ? data : null;

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
        <div>
          <h2 className="text-[14px] font-semibold text-gray-900">Token usage</h2>
          <p className="text-[11px] text-gray-400">Tokens and cost per provider and model.</p>
        </div>
        {shown ? (
          <div className="text-[11px] text-gray-400 sm:text-right">
            <div>Updated at {fmtUtcHour(shown.refreshed_at)} (hourly refresh)</div>
            {shown.range === '1h' ? (
              <div>showing hour {fmtUtcRange(shown.window.from, shown.window.to)}</div>
            ) : null}
          </div>
        ) : null}
      </div>

      {error ? (
        <div className="rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-600">{error}</div>
      ) : null}

      {shown ? (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            <Kpi label="Input" value={fmtCount(shown.totals.input_tokens)} />
            <Kpi label="Output" value={fmtCount(shown.totals.output_tokens)} />
            <Kpi label="Cached" value={fmtCount(shown.totals.cached_tokens)} />
            <Kpi label="Reasoning" value={fmtCount(shown.totals.reasoning_tokens)} />
            <Kpi label="Requests" value={fmtCount(shown.totals.request_count)} />
            <Kpi label="Cost USD" value={fmtCost(shown.totals.cost_usd)} />
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
      ) : !error ? (
        <KpiSkeleton />
      ) : null}
    </section>
  );
}

function KpiSkeleton() {
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="rounded-xl border border-gray-100 bg-gray-50 p-3">
          <div className="h-2.5 w-12 animate-pulse rounded bg-gray-200" />
          <div className="mt-2 h-5 w-16 animate-pulse rounded bg-gray-200" />
        </div>
      ))}
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

export function PerformanceMetricsSection({ refreshKey }: { refreshKey?: number }) {
  const {
    data,
    busy: perfMetricsLoading,
    load: loadPerformanceMetrics,
  } = useRefreshedAdminView(getPerformanceMetrics);
  const perfMetrics = data?.windows ?? [];

  // The server keeps these for a few minutes; an explicit refresh of the page
  // (refreshKey > 0) asks it to recompute, and the section shows the new ones
  // once computed.
  useEffect(() => {
    void loadPerformanceMetrics((refreshKey ?? 0) > 0);
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
        <div className="flex items-center gap-2">
          <ComputedAt at={data?.generated_at} refreshing={data?.refreshing} />
          {perfMetricsLoading && (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
        </div>
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
