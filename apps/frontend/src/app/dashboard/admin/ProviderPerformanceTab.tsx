'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
  Legend,
} from 'recharts';
import {
  AdminTtftScatterModel,
  ProviderStatsRow,
  getProviderStats,
  getTtftScatter,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

type RangeKey = '24h' | '7d' | '30d';

const RANGES: { key: RangeKey; label: string; days: number }[] = [
  { key: '24h', label: 'Last 24h', days: 1 },
  { key: '7d', label: 'Last 7d', days: 7 },
  { key: '30d', label: 'Last 30d', days: 30 },
];

function rangeWindow(key: RangeKey): { from: string; to: string } {
  const days = RANGES.find((r) => r.key === key)?.days ?? 7;
  const to = new Date();
  to.setMinutes(0, 0, 0);
  const from = new Date(to.getTime() - days * 24 * 60 * 60 * 1000);
  return { from: from.toISOString(), to: to.toISOString() };
}

function fmtHour(iso: string): string {
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:00`;
}

function TtftScatterCard({ model }: { model: AdminTtftScatterModel }) {
  const safePoints = model.points.filter((p) => p.prompt_tokens > 0);
  const cached = safePoints.filter((p) => p.cache_hit);
  const uncached = safePoints.filter((p) => !p.cache_hit);
  const heading = `${model.model_id} · ${model.provider}`;
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-baseline justify-between gap-3">
        <div className="truncate text-[13px] font-semibold text-gray-900" title={heading}>
          {heading}
        </div>
        <div className="shrink-0 text-[11px] text-gray-400 tabular-nums">
          {safePoints.length.toLocaleString()} pts
        </div>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-600">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-emerald-500 opacity-60" aria-hidden="true" />
          Cache hit ({cached.length.toLocaleString()})
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-slate-500 opacity-60" aria-hidden="true" />
          No cache ({uncached.length.toLocaleString()})
        </span>
      </div>
      <div className="mt-3 h-[260px]">
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart margin={{ top: 8, right: 12, bottom: 24, left: 12 }}>
            <CartesianGrid stroke="#f1f5f9" strokeDasharray="3 3" />
            <XAxis
              type="number"
              dataKey="prompt_tokens"
              name="Input length"
              scale="log"
              domain={['auto', 'auto']}
              allowDataOverflow
              tick={{ fontSize: 10, fill: '#6b7280' }}
              label={{
                value: 'Input length (tokens)',
                position: 'insideBottom',
                offset: -10,
                style: { fontSize: 11, fill: '#6b7280' },
              }}
            />
            <YAxis
              type="number"
              dataKey="ttft_ms"
              name="TTFT"
              tick={{ fontSize: 10, fill: '#6b7280' }}
              label={{
                value: 'TTFT (ms)',
                angle: -90,
                position: 'insideLeft',
                style: { fontSize: 11, fill: '#6b7280', textAnchor: 'middle' },
              }}
            />
            <ZAxis range={[18, 18]} />
            <Tooltip
              cursor={{ strokeDasharray: '3 3' }}
              contentStyle={{ fontSize: 11 }}
              labelFormatter={() => ''}
              wrapperStyle={{ outline: 'none' }}
            />
            <Scatter
              name="Cache hit"
              data={cached}
              fill="#10b981"
              fillOpacity={0.6}
              shape="circle"
            />
            <Scatter
              name="No cache"
              data={uncached}
              fill="#64748b"
              fillOpacity={0.6}
              shape="circle"
            />
          </ScatterChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function ModelPerformanceSection({ modelId, rows }: { modelId: string; rows: ProviderStatsRow[] }) {
  const chartData = useMemo(
    () =>
      rows.map((r) => ({
        t: fmtHour(r.hour_bucket),
        ttft_p50: r.ttft_p50_ms ?? null,
        ttft_p95: r.ttft_p95_ms ?? null,
        thru_avg: r.throughput_avg_tps ?? null,
        thru_p50: r.throughput_p50_tps ?? null,
        thru_p95: r.throughput_p95_tps ?? null,
      })),
    [rows],
  );

  const totals = useMemo(() => {
    const requests = rows.reduce((acc, r) => acc + r.request_count, 0);
    const errors = rows.reduce((acc, r) => acc + r.error_count, 0);
    const tokens = rows.reduce((acc, r) => acc + r.total_completion_tokens, 0);
    const errorRate = requests === 0 ? 0 : errors / requests;
    return { requests, errors, errorRate, tokens };
  }, [rows]);

  if (rows.length === 0) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h3 className="text-[14px] font-semibold text-gray-900">{modelId}</h3>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <Kpi label="Requests" value={totals.requests.toLocaleString()} />
        <Kpi label="Error rate" value={`${(totals.errorRate * 100).toFixed(2)}%`} />
        <Kpi label="Completion tokens" value={totals.tokens.toLocaleString()} />
      </div>

      <div
        data-testid="provider-performance-chart-row"
        className="grid grid-cols-1 gap-3 lg:grid-cols-2"
      >
        <div data-testid="provider-performance-ttft-card" className="rounded-xl border p-3">
          <p className="mb-1 text-[13px] font-semibold">TTFT (ms)</p>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="t" minTickGap={32} tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Line type="monotone" dataKey="ttft_p50" stroke="#3b82f6" dot={false} name="p50" />
                <Line type="monotone" dataKey="ttft_p95" stroke="#f59e0b" dot={false} name="p95" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div data-testid="provider-performance-throughput-card" className="rounded-xl border p-3">
          <p className="mb-1 text-[13px] font-semibold">Throughput (tokens/sec)</p>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="t" minTickGap={32} tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Line type="monotone" dataKey="thru_avg" stroke="#10b981" dot={false} name="avg" />
                <Line type="monotone" dataKey="thru_p50" stroke="#3b82f6" dot={false} name="p50" />
                <Line type="monotone" dataKey="thru_p95" stroke="#8b5cf6" dot={false} name="p95" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </div>
  );
}

export function ProviderPerformanceTab({ refreshKey = 0 }: { refreshKey?: number } = {}) {
  const [allProviders, setAllProviders] = useState<string[]>([]);
  const [allPairs, setAllPairs] = useState<{ provider: string; model_id: string }[]>([]);
  const [provider, setProvider] = useState<string>('');
  const [range, setRange] = useState<RangeKey>('7d');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [ttftScatter, setTtftScatter] = useState<AdminTtftScatterModel[]>([]);
  const [ttftScatterLoading, setTtftScatterLoading] = useState(false);
  const [ttftScatterError, setTtftScatterError] = useState<string | null>(null);
  const [modelRows, setModelRows] = useState<Record<string, ProviderStatsRow[]>>({});

  const loadTtftScatter = useCallback(async () => {
    setTtftScatterLoading(true);
    setTtftScatterError(null);
    try {
      const resp = await getTtftScatter();
      setTtftScatter(resp.models);
    } catch (exc) {
      setTtftScatterError(getErrorMessage(exc));
    } finally {
      setTtftScatterLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTtftScatter();
  }, [loadTtftScatter, refreshKey]);

  const providerModels = useMemo(
    () =>
      Array.from(new Set(allPairs.filter((p) => p.provider === provider).map((p) => p.model_id))),
    [allPairs, provider],
  );

  const loadData = useCallback(async (prov: string, rangeKey: RangeKey) => {
    if (!prov) return;
    setLoading(true);
    setError(null);
    setModelRows({});
    try {
      const window_ = rangeWindow(rangeKey);
      const resp = await getProviderStats({
        provider: prov,
        model_id: '__all__',
        from: window_.from,
        to: window_.to,
      });
      setAllProviders(resp.providers);
      setAllPairs(resp.pairs);
      const grouped: Record<string, ProviderStatsRow[]> = {};
      for (const row of resp.rows) {
        const key = row.model_id;
        (grouped[key] ??= []).push(row);
      }
      setModelRows(grouped);
    } catch (exc) {
      setError(getErrorMessage(exc));
    } finally {
      setLoading(false);
    }
  }, []);

  const initializedRef = useRef(false);
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;
    let cancelled = false;
    (async () => {
      try {
        const window_ = rangeWindow(range);
        const resp = await getProviderStats({
          provider: '__none__',
          model_id: '__none__',
          from: window_.from,
          to: window_.to,
        });
        if (cancelled) return;
        setAllProviders(resp.providers);
        setAllPairs(resp.pairs);
        if (resp.providers.length > 0) {
          setProvider(resp.providers[0]);
        }
      } catch (exc) {
        if (!cancelled) setError(getErrorMessage(exc));
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [range]);

  useEffect(() => {
    if (initializing) return;
    if (!provider) return;
    void loadData(provider, range);
  }, [provider, range, loadData, initializing, refreshKey]);

  const scatterForProvider = useMemo(
    () => ttftScatter.filter((m) => m.provider === provider),
    [ttftScatter, provider],
  );

  const overallTotals = useMemo(() => {
    const allRows = Object.values(modelRows).flat();
    const requests = allRows.reduce((acc, r) => acc + r.request_count, 0);
    const errors = allRows.reduce((acc, r) => acc + r.error_count, 0);
    const tokens = allRows.reduce((acc, r) => acc + r.total_completion_tokens, 0);
    const errorRate = requests === 0 ? 0 : errors / requests;
    return { requests, errors, errorRate, tokens };
  }, [modelRows]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3 items-end">
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Provider</span>
          <select
            className="border rounded px-2 py-1"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            {allProviders.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Range</span>
          <select
            className="border rounded px-2 py-1"
            value={range}
            onChange={(e) => setRange(e.target.value as RangeKey)}
          >
            {RANGES.map((r) => (
              <option key={r.key} value={r.key}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error ? <div className="text-red-600 text-sm">{error}</div> : null}
      {loading || initializing ? <div className="text-gray-500 text-sm">Loading…</div> : null}

      {!initializing && !loading && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Kpi label="Total requests" value={overallTotals.requests.toLocaleString()} />
          <Kpi label="Error rate" value={`${(overallTotals.errorRate * 100).toFixed(2)}%`} />
          <Kpi label="Completion tokens" value={overallTotals.tokens.toLocaleString()} />
        </div>
      )}

      {providerModels.map((modelId) => (
        <ModelPerformanceSection key={modelId} modelId={modelId} rows={modelRows[modelId] ?? []} />
      ))}

      <div>
        <div className="mb-3 flex items-center justify-between">
          <div>
            <h2 className="text-[15px] font-semibold text-gray-900">
              TTFT vs input length (last 1000 per model)
            </h2>
            <p className="text-[12px] text-gray-400">
              Per-model scatter of time-to-first-token against prompt length, split by prompt-cache
              hit.
            </p>
          </div>
          {ttftScatterLoading && (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
        </div>
        {ttftScatterError ? (
          <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-[13px] text-red-700">
              Failed to load scatter data: {ttftScatterError}
            </p>
          </div>
        ) : scatterForProvider.length > 0 ? (
          <div className="grid gap-3 lg:grid-cols-2">
            {scatterForProvider.map((m) => (
              <TtftScatterCard key={`${m.model_id}::${m.provider}`} model={m} />
            ))}
          </div>
        ) : !ttftScatterLoading ? (
          <div className="rounded-xl border border-dashed border-gray-200 py-8 text-center">
            <p className="text-[13px] text-gray-400">No scatter data available.</p>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border p-4">
      <p className="text-[11px] uppercase tracking-wide text-gray-400">{label}</p>
      <p className="mt-1 text-2xl font-bold text-gray-900">{value}</p>
    </div>
  );
}
