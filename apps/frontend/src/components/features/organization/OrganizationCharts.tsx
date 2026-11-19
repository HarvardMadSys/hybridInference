'use client';

// Loaded via next/dynamic from the organization pages. Do not import
// statically, or the recharts bundle lands in every page's first chunk.
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

/** One point of a series, with its label already in the reader's language. */
export interface SeriesPoint {
  label: string;
  value: number;
}

const SERIES_COLOR = '#3b82f6';

const TOOLTIP_STYLE = {
  fontSize: 12,
  borderRadius: 8,
  border: '1px solid #e5e7eb',
  boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
} as const;

const AXIS_TICK = { fontSize: 11, fill: '#9ca3af' } as const;
const MARGIN = { top: 8, right: 8, left: 0, bottom: 0 } as const;

/**
 * One series over the days or hours of a period: a line for a count that
 * moves (members with requests), bars for an amount that adds up (spend).
 *
 * The same figures are always available as a table beside the chart, so
 * nothing here is the only way to read them.
 */
export default function SeriesChart({
  kind,
  points,
  name,
  formatValue,
  height = 220,
}: {
  kind: 'line' | 'bar';
  points: SeriesPoint[];
  /** What the series measures, for the tooltip. */
  name: string;
  formatValue: (value: number) => string;
  height?: number;
}) {
  const axes = (
    <>
      <CartesianGrid stroke="#f3f4f6" vertical={false} />
      <XAxis
        dataKey="label"
        tick={AXIS_TICK}
        tickLine={false}
        axisLine={{ stroke: '#e5e7eb' }}
        minTickGap={24}
      />
      <YAxis
        tick={AXIS_TICK}
        tickLine={false}
        axisLine={false}
        width={52}
        allowDecimals={kind === 'bar'}
        tickFormatter={(value) => formatValue(Number(value))}
      />
      <Tooltip
        // Translucent, so the marker reads on the light surface and on the dark one.
        cursor={
          kind === 'bar' ? { fill: 'rgba(59,130,246,0.06)' } : { stroke: 'rgba(148,163,184,0.5)' }
        }
        contentStyle={TOOLTIP_STYLE}
        formatter={(value) => [formatValue(Number(value)), name]}
      />
    </>
  );
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        {kind === 'line' ? (
          <LineChart data={points} margin={MARGIN}>
            {axes}
            <Line
              type="linear"
              dataKey="value"
              name={name}
              stroke={SERIES_COLOR}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 3 }}
              isAnimationActive={false}
            />
          </LineChart>
        ) : (
          <BarChart data={points} margin={MARGIN}>
            {axes}
            <Bar
              dataKey="value"
              name={name}
              fill={SERIES_COLOR}
              radius={[3, 3, 0, 0]}
              maxBarSize={28}
              isAnimationActive={false}
            />
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
