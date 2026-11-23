'use client';

// Loaded via next/dynamic from the pages that show a series. Do not import
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
import {
  CHART_AXIS_LINE,
  CHART_AXIS_TICK,
  CHART_GRID_STROKE,
  CHART_TOOLTIP_STYLE,
} from './chartTheme';

/** One point of a series, with its label already in the reader's language. */
export interface SeriesPoint {
  label: string;
  value: number;
}

const SERIES_COLOR = '#3b82f6';

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
  formatTick = formatValue,
  height = 220,
}: {
  kind: 'line' | 'bar';
  points: SeriesPoint[];
  /** What the series measures, for the tooltip. */
  name: string;
  formatValue: (value: number) => string;
  /**
   * A value on the axis, which has room for a short text only: a long amount
   * would be cut at the edge of the chart. The tooltip has the value in full.
   */
  formatTick?: (value: number) => string;
  height?: number;
}) {
  const axes = (
    <>
      <CartesianGrid stroke={CHART_GRID_STROKE} vertical={false} />
      <XAxis
        dataKey="label"
        tick={CHART_AXIS_TICK}
        tickLine={false}
        axisLine={CHART_AXIS_LINE}
        minTickGap={24}
      />
      <YAxis
        tick={CHART_AXIS_TICK}
        tickLine={false}
        axisLine={false}
        width={52}
        allowDecimals={kind === 'bar'}
        tickFormatter={(value) => formatTick(Number(value))}
      />
      <Tooltip
        // Translucent, so the marker reads on the light surface and on the dark one.
        cursor={
          kind === 'bar' ? { fill: 'rgba(59,130,246,0.06)' } : { stroke: 'rgba(148,163,184,0.5)' }
        }
        contentStyle={CHART_TOOLTIP_STYLE}
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
              // One point has no line to draw: it is shown as a dot.
              dot={points.length === 1 ? { r: 3 } : false}
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
