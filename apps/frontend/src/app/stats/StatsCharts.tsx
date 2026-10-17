'use client';

// Loaded via next/dynamic from StatsContent. Do not import statically, or the
// recharts bundle lands back in the page's initial chunk.
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { ClientKind, PublicStats } from '@/lib/api/publicStats';
import { fill } from '@/lib/utils/interpolate';
import { KIND_COLORS, KIND_ORDER } from './format';
import { useStatsFormat } from './useStatsFormat';

const AXIS_TICK = { fontSize: 11, fill: '#9ca3af' };
const GRID = '#f3f4f6';
const TOOLTIP_STYLE = {
  borderRadius: 8,
  border: '1px solid #e5e7eb',
  fontSize: 12,
  boxShadow: '0 4px 12px rgba(0,0,0,0.08)',
};

export function DailyTokensChart({
  daily,
  metric,
}: {
  daily: PublicStats['daily'];
  metric: 'tokens' | 'requests';
}): JSX.Element {
  const f = useStatsFormat();
  const { t } = f;
  const data = daily.map((d, i) => ({
    ...d,
    tokens: d.input_tokens + d.output_tokens,
    partial: i === 0 || i === daily.length - 1,
  }));
  return (
    <div className="h-[240px]">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis
            dataKey="date"
            tickFormatter={(v) => f.day(v as string)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: '#e5e7eb' }}
            minTickGap={36}
          />
          <YAxis
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={44}
            tickFormatter={(v) => f.compact(v as number)}
          />
          <Tooltip
            cursor={{ fill: 'rgba(42,120,214,0.06)' }}
            contentStyle={TOOLTIP_STYLE}
            labelFormatter={(label, payload) =>
              payload?.[0]?.payload?.partial
                ? f.partialDay(label as string)
                : f.day(label as string)
            }
            formatter={(_value, _name, item) => {
              const d = item.payload as (typeof data)[number];
              return metric === 'tokens'
                ? [
                    fill(t('stats.tooltip_tokens', '{total} ({input} in, {output} out)'), {
                      total: f.compact(d.tokens),
                      input: f.compact(d.input_tokens),
                      output: f.compact(d.output_tokens),
                    }),
                    t('stats.tokens', 'Tokens'),
                  ]
                : [f.compact(d.requests), t('stats.requests', 'Requests')];
            }}
          />
          <Bar dataKey={metric} radius={[3, 3, 0, 0]} isAnimationActive={false} maxBarSize={24}>
            {data.map((d) => (
              // The first and last days are cut by the window; dim them so a
              // short bar does not read as a real drop.
              <Cell key={d.date} fill={KIND_COLORS.coding} fillOpacity={d.partial ? 0.35 : 1} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export interface WeeklySeries {
  key: string;
  name: string;
  color: string;
  values: number[];
}

export function WeeklyLinesChart({
  weeks,
  series,
  firstPartial,
  lastPartial,
}: {
  weeks: string[];
  series: WeeklySeries[];
  firstPartial: boolean;
  lastPartial: boolean;
}): JSX.Element {
  const data = weeks.map((week, i) => {
    const row: Record<string, string | number> = { week };
    for (const s of series) row[s.key] = s.values[i];
    return row;
  });
  const f = useStatsFormat();
  const label = (week: string) => f.week(week, weeks, firstPartial, lastPartial);
  return (
    <div className="h-[200px]">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis
            dataKey="week"
            tickFormatter={(v) => f.day(v as string)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: '#e5e7eb' }}
            minTickGap={28}
          />
          <YAxis
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={36}
            allowDecimals={false}
            domain={[0, 'auto']}
          />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={(w) => label(w as string)} />
          {series.map((s) => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name}
              stroke={s.color}
              strokeWidth={2}
              dot={{ r: 3, fill: s.color, strokeWidth: 0 }}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function KindShareChart({
  weeks,
  kindWeekly,
  firstPartial,
  lastPartial,
}: {
  weeks: string[];
  kindWeekly: Record<ClientKind, number>[];
  firstPartial: boolean;
  lastPartial: boolean;
}): JSX.Element {
  const f = useStatsFormat();
  const data = weeks.map((week, i) => ({ week, ...kindWeekly[i] }));
  return (
    <div className="h-[200px]">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis
            dataKey="week"
            tickFormatter={(v) => f.day(v as string)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: '#e5e7eb' }}
            minTickGap={28}
          />
          <YAxis
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={40}
            domain={[0, 1]}
            ticks={[0, 0.25, 0.5, 0.75, 1]}
            tickFormatter={(v) => `${Math.round((v as number) * 100)}%`}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            cursor={{ fill: 'rgba(0,0,0,0.03)' }}
            labelFormatter={(w) => f.week(w as string, weeks, firstPartial, lastPartial)}
            formatter={(value, name) => [f.percent(value as number), name as string]}
          />
          {KIND_ORDER.map((kind) => (
            <Bar
              key={kind}
              dataKey={kind}
              name={f.kind(kind)}
              stackId="share"
              fill={KIND_COLORS[kind]}
              stroke="#ffffff"
              strokeWidth={1}
              maxBarSize={28}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
