'use client';

import { Line, LineChart, ResponsiveContainer, YAxis } from 'recharts';

/**
 * A row's small trend line: one value for each day, the oldest first. It has
 * no axis and no figures; the columns beside it carry the amounts.
 */
export function Sparkline({
  values,
  label,
  width = 60,
  height = 16,
  color = '#9ca3af', // neutral grey-400
}: {
  values: Array<number | null>;
  /** What the line shows, for a screen reader. */
  label: string;
  width?: number;
  height?: number;
  color?: string;
}) {
  if (!values || values.length === 0) {
    return <span className="text-xs text-gray-300">—</span>;
  }
  const data = values.map((value, index) => ({ index, value }));
  return (
    <div style={{ width, height }} aria-label={label} role="img">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <YAxis hide domain={[0, 'dataMax']} />
          <Line
            type="monotone"
            dataKey="value"
            stroke={color}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
