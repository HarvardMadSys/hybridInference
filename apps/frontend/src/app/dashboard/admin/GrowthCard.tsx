'use client';

// Loaded via next/dynamic from AnalyticsTab — do not import statically, or the
// recharts bundle lands back in the tab's initial chunk.
import { useEffect, useMemo, useState } from 'react';
import {
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { AdminGrowthResponse, GrowthRange, GrowthTrend, getGrowthAnalytics } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const RANGES: GrowthRange[] = [30, 60, 90];

const MA_WINDOW = 7;

const DAILY_COLOR = '#3b82f6';
const CUMULATIVE_COLOR = '#94a3b8';
const MA_COLOR = '#1d4ed8';

const compact = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});
const numberFmt = new Intl.NumberFormat('en-US');

function fmtCount(n: number): string {
  return Math.abs(n) >= 10_000 ? compact.format(n) : numberFmt.format(Math.round(n));
}

function fmtSigned(n: number): string {
  if (n === 0) return '0';
  const sign = n > 0 ? '+' : '−';
  const magnitude = Math.abs(n);
  // Sub-10 slopes lose their whole meaning when rounded to an integer.
  const body = magnitude >= 10 ? fmtCount(magnitude) : magnitude.toFixed(1);
  return `${sign}${body}`;
}

function fmtDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

const TOOLTIP_STYLE = {
  fontSize: 12,
  borderRadius: 8,
  border: '1px solid #e5e7eb',
  boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
} as const;

interface SeriesPoint {
  day: string;
  value: number;
  /** Null on the partial day, whose short value would dip the mean. */
  ma: number | null;
  cumulative: number;
  partial: boolean;
}

/**
 * `cumulativePick` is separate from `pick` because for DAU the running total has
 * to sum `new_users`: summing DAU would count the same person once per active day.
 */
function toSeries(
  points: AdminGrowthResponse['points'],
  pick: (p: AdminGrowthResponse['points'][number]) => number,
  cumulativePick: (p: AdminGrowthResponse['points'][number]) => number,
): SeriesPoint[] {
  let running = 0;
  const complete: number[] = [];
  return points.map((p) => {
    const value = pick(p);
    running += cumulativePick(p);
    let ma: number | null = null;
    if (!p.partial) {
      complete.push(value);
      const window = complete.slice(-MA_WINDOW);
      ma = window.reduce((acc, v) => acc + v, 0) / window.length;
    }
    return { day: p.day, value, ma, cumulative: running, partial: p.partial };
  });
}

function ChangeBadge({ trend }: { trend: GrowthTrend }) {
  if (trend.change_pct == null || trend.compare_days === 0) {
    // Growth from a flat zero has no percentage, so this is the only place the
    // recent level shows as a number.
    return (
      <span className="text-[11px] text-gray-400">
        no baseline · recent {trend.compare_days}d avg {fmtCount(trend.recent_avg)}
      </span>
    );
  }
  const pct = trend.change_pct * 100;
  const flat = Math.abs(pct) < 0.5;
  const tone = flat
    ? 'bg-gray-100 text-gray-600'
    : pct > 0
      ? 'bg-emerald-50 text-emerald-700'
      : 'bg-red-50 text-red-600';
  const arrow = flat ? '→' : pct > 0 ? '▲' : '▼';
  return (
    <span className="flex items-center gap-1.5">
      <span className={`rounded px-1.5 py-0.5 text-[11px] font-semibold tabular-nums ${tone}`}>
        {arrow} {Math.abs(pct).toFixed(pct >= 100 ? 0 : 1)}%
      </span>
      <span className="text-[11px] text-gray-400">vs previous {trend.compare_days}d</span>
    </span>
  );
}

function LegendDot({
  color,
  dashed = false,
  label,
}: {
  color: string;
  dashed?: boolean;
  label: string;
}) {
  const dash = `repeating-linear-gradient(90deg, ${color} 0 3px, transparent 3px 5px)`;
  return (
    <span className="flex items-center gap-1.5 text-[11px] text-gray-500">
      <span
        className="h-0.5 w-3 shrink-0 rounded-full"
        style={dashed ? { backgroundImage: dash } : { background: color }}
      />
      {label}
    </span>
  );
}

function GrowthChart({
  series,
  trend,
  title,
  unit,
  dailyLabel,
  cumulativeLabel,
}: {
  series: SeriesPoint[];
  trend: GrowthTrend;
  title: string;
  unit: string;
  dailyLabel: string;
  cumulativeLabel: string;
}) {
  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex items-baseline gap-3">
          <p className="text-[13px] font-semibold text-gray-900">{title}</p>
          <p className="text-[22px] font-bold leading-none tracking-tight text-gray-900 tabular-nums">
            {fmtSigned(trend.slope_per_day)}
          </p>
          <p className="text-xs text-gray-500">{unit}/day</p>
        </div>
        <ChangeBadge trend={trend} />
      </div>

      <div className="mt-3 h-[190px]">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={series} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="#f3f4f6" vertical={false} />
            <XAxis
              dataKey="day"
              tickFormatter={fmtDay}
              tick={{ fontSize: 11, fill: '#9ca3af' }}
              tickLine={false}
              axisLine={{ stroke: '#e5e7eb' }}
              minTickGap={28}
            />
            <YAxis
              yAxisId="daily"
              tick={{ fontSize: 11, fill: '#9ca3af' }}
              tickLine={false}
              axisLine={false}
              width={44}
              tickFormatter={(v) => fmtCount(v as number)}
            />
            <YAxis
              yAxisId="cumulative"
              orientation="right"
              tick={{ fontSize: 11, fill: '#cbd5e1' }}
              tickLine={false}
              axisLine={false}
              width={44}
              tickFormatter={(v) => fmtCount(v as number)}
            />
            <Tooltip
              cursor={{ fill: 'rgba(59,130,246,0.06)' }}
              // The last bar is dimmed because today is still filling. Nothing
              // else on the card says so, so the tooltip has to.
              labelFormatter={(label, payload) =>
                payload?.[0]?.payload?.partial
                  ? `${fmtDay(label as string)} · partial`
                  : fmtDay(label as string)
              }
              formatter={(value, name) => {
                if (value == null) return ['—', name as string];
                return [fmtCount(value as number), name as string];
              }}
              contentStyle={TOOLTIP_STYLE}
            />
            <Bar
              yAxisId="daily"
              dataKey="value"
              name={dailyLabel}
              radius={[2, 2, 0, 0]}
              isAnimationActive={false}
            >
              {series.map((p, i) => (
                // Today is still filling; dim it so its short bar does not read
                // as a real drop.
                <Cell key={i} fill={DAILY_COLOR} fillOpacity={p.partial ? 0.3 : 0.85} />
              ))}
            </Bar>
            <Line
              yAxisId="daily"
              type="monotone"
              dataKey="ma"
              name={`${MA_WINDOW}-day avg`}
              stroke={MA_COLOR}
              strokeWidth={2}
              strokeDasharray="4 3"
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
            <Line
              yAxisId="cumulative"
              type="monotone"
              dataKey="cumulative"
              name={cumulativeLabel}
              stroke={CUMULATIVE_COLOR}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        <LegendDot color={DAILY_COLOR} label={`${dailyLabel} (left)`} />
        <LegendDot color={MA_COLOR} dashed label={`${MA_WINDOW}-day avg (left)`} />
        <LegendDot color={CUMULATIVE_COLOR} label={`${cumulativeLabel} (right)`} />
      </div>
    </div>
  );
}

export default function GrowthCard() {
  const [range, setRange] = useState<GrowthRange>(30);
  const [data, setData] = useState<AdminGrowthResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Aborting on range change keeps a slow earlier request from landing on top of
  // a newer one. The `cancelled` flag guards the state writes as well: an abort
  // races the response, so a superseded call must not clear `loading` either —
  // that would blank the skeleton while the current range is still in flight.
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    setLoading(true);
    setError(null);
    (async () => {
      try {
        const resp = await getGrowthAnalytics(range, { signal: controller.signal });
        if (!cancelled) setData(resp);
      } catch (e) {
        if (!cancelled) setError(getErrorMessage(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [range]);

  const userSeries = useMemo(
    () =>
      toSeries(
        data?.points ?? [],
        (p) => p.active_users,
        (p) => p.new_users,
      ),
    [data],
  );
  const tokenSeries = useMemo(
    () =>
      toSeries(
        data?.points ?? [],
        (p) => p.tokens,
        (p) => p.tokens,
      ),
    [data],
  );

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 sm:col-span-2">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">Growth Rate</p>
        <div
          aria-label="Growth range"
          className="inline-flex overflow-hidden rounded-lg border border-gray-200"
          role="group"
        >
          {RANGES.map((r) => (
            <button
              key={r}
              onClick={() => setRange(r)}
              aria-pressed={range === r}
              className={`px-2.5 py-1 text-[12px] font-medium transition ${
                range === r
                  ? 'bg-gray-900 text-white'
                  : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
              }`}
            >
              {r}d
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-600">{error}</div>
      )}

      {!error && (loading || !data) && (
        <div className="space-y-6">
          <div className="h-[240px] animate-pulse rounded-lg bg-gray-100" />
          <div className="h-[240px] animate-pulse rounded-lg bg-gray-100" />
        </div>
      )}

      {!error && !loading && data && (
        <div className="space-y-7">
          <GrowthChart
            series={userSeries}
            trend={data.users_trend}
            title="Active users"
            unit="users"
            dailyLabel="DAU"
            cumulativeLabel="cumulative users"
          />
          <GrowthChart
            series={tokenSeries}
            trend={data.tokens_trend}
            title="Token consumption"
            unit="tokens"
            dailyLabel="tokens / day"
            cumulativeLabel="cumulative tokens"
          />
        </div>
      )}
    </div>
  );
}
