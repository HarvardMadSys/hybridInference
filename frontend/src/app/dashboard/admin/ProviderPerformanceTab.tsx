'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Legend,
} from 'recharts';
import { ProviderStatsResponse, ProviderStatsRow, getProviderStats } from '@/lib/api/admin';
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

export function ProviderPerformanceTab() {
  const [data, setData] = useState<ProviderStatsResponse | null>(null);
  const [allProviders, setAllProviders] = useState<string[]>([]);
  const [allPairs, setAllPairs] = useState<{ provider: string; model_id: string }[]>([]);
  const [provider, setProvider] = useState<string>('');
  const [model, setModel] = useState<string>('');
  const [range, setRange] = useState<RangeKey>('7d');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [initializing, setInitializing] = useState(true);

  const modelsForProvider = useMemo(
    () =>
      allPairs
        .filter((p) => p.provider === provider)
        .map((p) => p.model_id)
        .filter((v, i, a) => a.indexOf(v) === i),
    [allPairs, provider],
  );

  const fetchFilters = useCallback(
    async (rangeKey: RangeKey) => {
      const window_ = rangeWindow(rangeKey);
      const resp = await getProviderStats({
        provider: '__none__',
        model_id: '__none__',
        from: window_.from,
        to: window_.to,
      });
      setAllProviders(resp.providers);
      setAllPairs(resp.pairs);
      return resp.pairs;
    },
    [],
  );

  const loadData = useCallback(
    async (prov: string, mod: string, rangeKey: RangeKey) => {
      if (!prov || !mod) return;
      setLoading(true);
      setError(null);
      try {
        const window_ = rangeWindow(rangeKey);
        const resp = await getProviderStats({
          provider: prov,
          model_id: mod,
          from: window_.from,
          to: window_.to,
        });
        setData(resp);
        setAllProviders(resp.providers);
        setAllPairs(resp.pairs);
      } catch (exc) {
        setError(getErrorMessage(exc));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const initializedRef = useRef(false);
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;
    let cancelled = false;
    (async () => {
      try {
        const pairs = await fetchFilters(range);
        if (cancelled) return;
        if (pairs.length > 0) {
          setProvider(pairs[0].provider);
          setModel(pairs[0].model_id);
          await loadData(pairs[0].provider, pairs[0].model_id, range);
        }
      } catch (exc) {
        if (!cancelled) setError(getErrorMessage(exc));
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    return () => { cancelled = true; };
  }, [range, fetchFilters, loadData]);

  const prevRangeRef = useRef(range);
  useEffect(() => {
    if (prevRangeRef.current === range) return;
    prevRangeRef.current = range;
    if (!provider || !model) return;
    void loadData(provider, model, range);
  }, [range, provider, model, loadData]);

  useEffect(() => {
    if (initializing) return;
    if (!provider || !model) return;
    void loadData(provider, model, range);
  }, [provider, model, range, loadData, initializing]);

  const chartData = useMemo(
    () =>
      (data?.rows ?? []).map((r: ProviderStatsRow) => ({
        t: fmtHour(r.hour_bucket),
        ttft_p50: r.ttft_p50_ms ?? null,
        ttft_p95: r.ttft_p95_ms ?? null,
        ttft_p99: r.ttft_p99_ms ?? null,
        thru_avg: r.throughput_avg_tps ?? null,
        thru_p50: r.throughput_p50_tps ?? null,
        thru_p95: r.throughput_p95_tps ?? null,
      })),
    [data],
  );

  const totals = useMemo(() => {
    const rows = data?.rows ?? [];
    const requests = rows.reduce((acc, r) => acc + r.request_count, 0);
    const errors = rows.reduce((acc, r) => acc + r.error_count, 0);
    const tokens = rows.reduce((acc, r) => acc + r.total_completion_tokens, 0);
    const errorRate = requests === 0 ? 0 : errors / requests;
    return { requests, errors, errorRate, tokens };
  }, [data]);

  const handleProviderChange = useCallback(
    (newProvider: string) => {
      setProvider(newProvider);
      const matching = allPairs
        .filter((p) => p.provider === newProvider)
        .map((p) => p.model_id);
      const unique = matching.filter((v, i, a) => a.indexOf(v) === i);
      if (unique.length > 0 && !unique.includes(model)) {
        setModel(unique[0]);
      }
    },
    [allPairs, model],
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3 items-end">
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Provider</span>
          <select
            className="border rounded px-2 py-1"
            value={provider}
            onChange={(e) => handleProviderChange(e.target.value)}
          >
            {allProviders.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Model</span>
          <select
            className="border rounded px-2 py-1"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            {modelsForProvider.map((m) => (
              <option key={m} value={m}>
                {m}
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
      {(loading || initializing) ? <div className="text-gray-500 text-sm">Loading…</div> : null}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Kpi label="Requests" value={totals.requests.toLocaleString()} />
        <Kpi label="Error rate" value={`${(totals.errorRate * 100).toFixed(2)}%`} />
        <Kpi label="Completion tokens" value={totals.tokens.toLocaleString()} />
      </div>

      <div className="rounded-xl border p-4">
        <p className="text-sm font-semibold mb-2">TTFT (ms)</p>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="t" minTickGap={32} />
              <YAxis />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="ttft_p50" stroke="#3b82f6" dot={false} name="p50" />
              <Line type="monotone" dataKey="ttft_p95" stroke="#f59e0b" dot={false} name="p95" />
              <Line type="monotone" dataKey="ttft_p99" stroke="#ef4444" dot={false} name="p99" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="rounded-xl border p-4">
        <p className="text-sm font-semibold mb-2">Throughput (tokens/sec)</p>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="t" minTickGap={32} />
              <YAxis />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="thru_avg" stroke="#10b981" dot={false} name="avg" />
              <Line type="monotone" dataKey="thru_p50" stroke="#3b82f6" dot={false} name="p50" />
              <Line type="monotone" dataKey="thru_p95" stroke="#8b5cf6" dot={false} name="p95" />
            </LineChart>
          </ResponsiveContainer>
        </div>
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
