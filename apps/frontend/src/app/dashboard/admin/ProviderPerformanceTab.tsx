'use client';

import {
  type MouseEvent as ReactMouseEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
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

function fmt2(v: unknown): string {
  if (v == null) return '';
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n.toFixed(2) : String(v);
}

type AxisDomain = [number, number] | ['auto', 'auto'];
type Scale = 'linear' | 'log';

const Y_AXIS_WIDTH = 48;
const CHART_MARGIN_LEFT = 12;
const CHART_MARGIN_RIGHT = 12;
const CHART_MARGIN_TOP = 8;
const CHART_MARGIN_BOTTOM = 24;
const X_AXIS_HEIGHT = 30;
const THROUGHPUT_Y_MAX = 280;

function TtftScatterCard({ model }: { model: AdminTtftScatterModel }) {
  const safePoints = model.points.filter((p) => p.prompt_tokens > 0);
  const cached = safePoints.filter((p) => p.cache_hit);
  const uncached = safePoints.filter((p) => !p.cache_hit);
  const heading = `${model.model_id} · ${model.provider}`;

  const [xDomain, setXDomain] = useState<AxisDomain>(['auto', 'auto']);
  const [yDomain, setYDomain] = useState<AxisDomain>(['auto', 'auto']);
  const [xScale, setXScale] = useState<Scale>('log');
  const [yScale, setYScale] = useState<Scale>('linear');
  const [dragStartX, setDragStartX] = useState<number | null>(null);
  const [dragEndX, setDragEndX] = useState<number | null>(null);
  const [dragStartY, setDragStartY] = useState<number | null>(null);
  const [dragEndY, setDragEndY] = useState<number | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const zoomed = xDomain[0] !== 'auto' || yDomain[0] !== 'auto';

  const dataXMin = useMemo(() => {
    if (safePoints.length === 0) return 1;
    return Math.max(
      1,
      safePoints.reduce((m, p) => Math.min(m, p.prompt_tokens), Infinity),
    );
  }, [safePoints]);
  const dataXMax = useMemo(() => {
    if (safePoints.length === 0) return 10;
    return safePoints.reduce((m, p) => Math.max(m, p.prompt_tokens), 0);
  }, [safePoints]);
  const dataYMin = useMemo(() => {
    if (safePoints.length === 0) return 0;
    return safePoints.reduce((m, p) => Math.min(m, p.ttft_ms), Infinity);
  }, [safePoints]);
  const dataYMax = useMemo(() => {
    if (safePoints.length === 0) return 1;
    return safePoints.reduce((m, p) => Math.max(m, p.ttft_ms), 0);
  }, [safePoints]);

  const visibleXMin = xDomain[0] === 'auto' ? dataXMin : (xDomain[0] as number);
  const visibleXMax = xDomain[1] === 'auto' ? dataXMax : (xDomain[1] as number);

  const xTicks = useMemo(() => {
    const candidates = [
      100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000,
      2000000, 5000000,
    ];
    const visible = candidates.filter((t) => t >= visibleXMin && t <= visibleXMax);
    const MAX_TICKS = 8;
    if (visible.length <= MAX_TICKS) return visible;
    const stride = Math.ceil(visible.length / MAX_TICKS);
    return visible.filter((_, i) => i % stride === 0);
  }, [visibleXMin, visibleXMax]);
  const visibleYMin = yDomain[0] === 'auto' ? dataYMin : (yDomain[0] as number);
  const visibleYMax = yDomain[1] === 'auto' ? dataYMax : (yDomain[1] as number);

  const pixelToX = (clientX: number): number | null => {
    const el = wrapperRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    const innerLeft = CHART_MARGIN_LEFT + Y_AXIS_WIDTH;
    const innerRight = rect.width - CHART_MARGIN_RIGHT;
    const innerWidth = innerRight - innerLeft;
    if (innerWidth <= 0) return null;
    const px = clientX - rect.left;
    const frac = Math.min(1, Math.max(0, (px - innerLeft) / innerWidth));
    if (xScale === 'log') {
      const lo = Math.log10(Math.max(visibleXMin, 1));
      const hi = Math.log10(Math.max(visibleXMax, visibleXMin + 1));
      return Math.pow(10, lo + frac * (hi - lo));
    }
    return visibleXMin + frac * (visibleXMax - visibleXMin);
  };

  const pixelToY = (clientY: number): number | null => {
    const el = wrapperRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    const innerTop = CHART_MARGIN_TOP;
    const innerBottom = rect.height - CHART_MARGIN_BOTTOM - X_AXIS_HEIGHT;
    const innerHeight = innerBottom - innerTop;
    if (innerHeight <= 0) return null;
    const py = clientY - rect.top;
    const frac = Math.min(1, Math.max(0, (py - innerTop) / innerHeight));
    if (yScale === 'log') {
      const lo = Math.log10(Math.max(visibleYMin, 1));
      const hi = Math.log10(Math.max(visibleYMax, visibleYMin + 1));
      return Math.pow(10, hi - frac * (hi - lo));
    }
    return visibleYMax - frac * (visibleYMax - visibleYMin);
  };

  const onDown = (e: ReactMouseEvent<HTMLDivElement>) => {
    const x = pixelToX(e.clientX);
    const y = pixelToY(e.clientY);
    if (x == null || y == null) return;
    e.preventDefault();
    setDragStartX(x);
    setDragEndX(x);
    setDragStartY(y);
    setDragEndY(y);
  };
  const onMove = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (dragStartX == null) return;
    const x = pixelToX(e.clientX);
    const y = pixelToY(e.clientY);
    if (x == null || y == null) return;
    setDragEndX(x);
    setDragEndY(y);
  };
  const onUp = () => {
    if (dragStartX != null && dragEndX != null) {
      const x1 = Math.min(dragStartX, dragEndX);
      const x2 = Math.max(dragStartX, dragEndX);
      const xMeaningful =
        xScale === 'log'
          ? x2 / Math.max(x1, 1) > 1.05
          : x2 - x1 > Math.max(visibleXMax - visibleXMin, 1) * 0.05;
      if (xMeaningful) {
        setXDomain([xScale === 'log' ? Math.max(x1, 1) : x1, x2]);
      }
    }
    if (dragStartY != null && dragEndY != null) {
      const y1 = Math.min(dragStartY, dragEndY);
      const y2 = Math.max(dragStartY, dragEndY);
      const yMeaningful =
        yScale === 'log'
          ? y2 / Math.max(y1, 1) > 1.05
          : y2 - y1 > Math.max(visibleYMax - visibleYMin, 1) * 0.05;
      if (yMeaningful) {
        setYDomain([yScale === 'log' ? Math.max(y1, 1) : y1, y2]);
      }
    }
    setDragStartX(null);
    setDragEndX(null);
    setDragStartY(null);
    setDragEndY(null);
  };
  const reset = () => {
    setXDomain(['auto', 'auto']);
    setYDomain(['auto', 'auto']);
  };

  const exportCsv = () => {
    const header = 'timestamp,prompt_tokens,ttft_ms,cache_hit\n';
    const body = safePoints
      .map(
        (p) => `${p.timestamp},${p.prompt_tokens},${p.ttft_ms},${p.cache_hit ? 'true' : 'false'}`,
      )
      .join('\n');
    const blob = new Blob([`${header}${body}\n`], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const safeName = `${model.model_id}_${model.provider}`.replace(/[^A-Za-z0-9._-]+/g, '_');
    const a = document.createElement('a');
    a.href = url;
    a.download = `ttft_${safeName}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const showDragArea =
    dragStartX != null &&
    dragEndX != null &&
    dragStartY != null &&
    dragEndY != null &&
    (dragStartX !== dragEndX || dragStartY !== dragEndY);

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-baseline justify-between gap-3">
        <div className="truncate text-[13px] font-semibold text-gray-900" title={heading}>
          {heading}
        </div>
        <div className="flex items-center gap-2">
          {zoomed && (
            <button
              type="button"
              onClick={reset}
              className="rounded border border-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50"
            >
              Reset zoom
            </button>
          )}
          <button
            type="button"
            onClick={exportCsv}
            disabled={safePoints.length === 0}
            className="rounded border border-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50 disabled:opacity-50 disabled:hover:bg-white"
          >
            Export CSV
          </button>
          <div className="shrink-0 text-[11px] text-gray-400 tabular-nums">
            {safePoints.length.toLocaleString()} pts
          </div>
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
        <span className="text-gray-400">· drag to zoom</span>
        <ScaleToggle label="X" value={xScale} onChange={setXScale} />
        <ScaleToggle label="Y" value={yScale} onChange={setYScale} />
      </div>
      <div
        ref={wrapperRef}
        className="relative mt-3 h-[260px] select-none cursor-crosshair"
        onMouseDown={onDown}
        onMouseMove={onMove}
        onMouseUp={onUp}
        onMouseLeave={() => {
          setDragStartX(null);
          setDragEndX(null);
          setDragStartY(null);
          setDragEndY(null);
        }}
      >
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart
            margin={{
              top: CHART_MARGIN_TOP,
              right: CHART_MARGIN_RIGHT,
              bottom: CHART_MARGIN_BOTTOM,
              left: CHART_MARGIN_LEFT,
            }}
          >
            <CartesianGrid stroke="#f1f5f9" strokeDasharray="3 3" />
            <XAxis
              type="number"
              dataKey="prompt_tokens"
              name="Input length"
              scale={xScale}
              domain={xDomain}
              allowDataOverflow
              height={X_AXIS_HEIGHT}
              ticks={xScale === 'log' && xTicks.length > 0 ? xTicks : undefined}
              tickFormatter={(v: number) => Number(v).toLocaleString()}
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
              width={Y_AXIS_WIDTH}
              scale={yScale}
              domain={yScale === 'log' ? [Math.max(visibleYMin, 1), visibleYMax] : yDomain}
              allowDataOverflow
              tick={{ fontSize: 10, fill: '#6b7280' }}
              label={{
                value: 'TTFT (ms)',
                angle: -90,
                position: 'insideLeft',
                style: { fontSize: 11, fill: '#6b7280', textAnchor: 'middle' },
              }}
            />
            <ZAxis range={[8, 8]} />
            <Tooltip
              cursor={{ strokeDasharray: '3 3' }}
              contentStyle={{ fontSize: 11 }}
              labelFormatter={() => ''}
              formatter={(v) => fmt2(v)}
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
            {showDragArea ? (
              <ReferenceArea
                x1={dragStartX as number}
                x2={dragEndX as number}
                y1={dragStartY as number}
                y2={dragEndY as number}
                fill="#3b82f6"
                fillOpacity={0.1}
                stroke="#3b82f6"
                strokeOpacity={0.4}
              />
            ) : null}
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
    const completion = rows.reduce((acc, r) => acc + r.total_completion_tokens, 0);
    const prefill = rows.reduce((acc, r) => acc + (r.total_prompt_tokens ?? 0), 0);
    const reasoning = rows.reduce((acc, r) => acc + (r.total_reasoning_tokens ?? 0), 0);
    const decode = Math.max(completion - reasoning, 0);
    const errorRate = requests === 0 ? 0 : errors / requests;
    return { requests, errors, errorRate, prefill, reasoning, decode };
  }, [rows]);

  if (rows.length === 0) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h3 className="text-lg font-semibold text-gray-900">{modelId}</h3>
      </div>

      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-[13px] text-gray-700">
        <Kpi label="Requests" value={totals.requests.toLocaleString()} />
        <Kpi label="Error rate" value={`${(totals.errorRate * 100).toFixed(2)}%`} />
        <Kpi label="Prefill" value={totals.prefill.toLocaleString()} />
        <Kpi label="Reasoning" value={totals.reasoning.toLocaleString()} />
        <Kpi label="Decode" value={totals.decode.toLocaleString()} />
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
                <YAxis tick={{ fontSize: 11 }} tickFormatter={fmt2} />
                <Tooltip formatter={(v) => fmt2(v)} />
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
                <YAxis
                  tick={{ fontSize: 11 }}
                  tickFormatter={fmt2}
                  domain={[0, THROUGHPUT_Y_MAX]}
                  allowDataOverflow
                />
                <Tooltip formatter={(v) => fmt2(v)} />
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
        // Prefer a provider that actually has data in the selected window so
        // the tab doesn't open empty; fall back to the full list otherwise.
        const candidates =
          resp.window_providers.length > 0 ? resp.window_providers : resp.providers;
        if (candidates.length > 0) {
          const preferred = candidates.includes('minimax') ? 'minimax' : candidates[0];
          setProvider(preferred);
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
    const completion = allRows.reduce((acc, r) => acc + r.total_completion_tokens, 0);
    const prefill = allRows.reduce((acc, r) => acc + (r.total_prompt_tokens ?? 0), 0);
    const reasoning = allRows.reduce((acc, r) => acc + (r.total_reasoning_tokens ?? 0), 0);
    const decode = Math.max(completion - reasoning, 0);
    const errorRate = requests === 0 ? 0 : errors / requests;
    return { requests, errors, errorRate, prefill, reasoning, decode };
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
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 rounded-lg border px-3 py-2 text-[13px]">
          <Kpi label="Total requests" value={overallTotals.requests.toLocaleString()} />
          <Kpi label="Error rate" value={`${(overallTotals.errorRate * 100).toFixed(2)}%`} />
          <Kpi label="Prefill" value={overallTotals.prefill.toLocaleString()} />
          <Kpi label="Reasoning" value={overallTotals.reasoning.toLocaleString()} />
          <Kpi label="Decode" value={overallTotals.decode.toLocaleString()} />
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

function ScaleToggle({
  label,
  value,
  onChange,
}: {
  label: string;
  value: Scale;
  onChange: (s: Scale) => void;
}) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="text-gray-400">{label}:</span>
      <span className="inline-flex overflow-hidden rounded border border-gray-200">
        {(['linear', 'log'] as Scale[]).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onChange(s)}
            className={
              value === s
                ? 'bg-gray-900 px-1.5 py-0.5 text-[10px] text-white'
                : 'px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50'
            }
          >
            {s}
          </button>
        ))}
      </span>
    </span>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="inline-flex items-baseline gap-1.5">
      <span className="text-[11px] uppercase tracking-wide text-gray-400">{label}</span>
      <span className="text-sm font-semibold tabular-nums text-gray-900">{value}</span>
    </div>
  );
}
