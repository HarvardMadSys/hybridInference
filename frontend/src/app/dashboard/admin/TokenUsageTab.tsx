'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ProviderTokenUsageResponse,
  ProviderTokenUsageRow,
  TokenUsageRange,
  getProviderTokenUsage,
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
): { provider: string; rows: ProviderTokenUsageRow[]; total: number }[] {
  const buckets = new Map<string, ProviderTokenUsageRow[]>();
  for (const r of rows) {
    const arr = buckets.get(r.provider) ?? [];
    arr.push(r);
    buckets.set(r.provider, arr);
  }
  const out: { provider: string; rows: ProviderTokenUsageRow[]; total: number }[] = [];
  for (const [provider, providerRows] of buckets) {
    const total = providerRows.reduce((acc, r) => acc + rowTotal(r), 0);
    out.push({ provider, rows: providerRows, total });
  }
  out.sort((a, b) => b.total - a.total);
  return out;
}

export function TokenUsageTab() {
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
  }, [load]);

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
            <div>
              Updated at {fmtUtcHour(data.refreshed_at)} (hourly refresh)
            </div>
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
                <ProviderTable key={g.provider} provider={g.provider} rows={g.rows} />
              ))}
            </div>
          )}
        </>
      ) : null}
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

function ProviderTable({
  provider,
  rows,
}: {
  provider: string;
  rows: ProviderTokenUsageRow[];
}) {
  return (
    <div className="rounded-xl border overflow-hidden">
      <div className="bg-gray-50 px-4 py-2 text-[13px] font-semibold text-gray-900">
        {provider}
      </div>
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
              <td className="px-3 py-2 text-right tabular-nums">
                {fmtCount(r.reasoning_tokens)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.request_count)}</td>
              <td className="px-4 py-2 text-right tabular-nums">{fmtCost(r.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
