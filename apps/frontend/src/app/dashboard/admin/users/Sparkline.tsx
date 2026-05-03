'use client';

import { Line, LineChart, ResponsiveContainer, YAxis } from 'recharts';
import type { CostHistoryPoint } from './types';

interface SparklineProps {
  points: CostHistoryPoint[];
  width?: number;
  height?: number;
  color?: string;
}

export function Sparkline({
  points,
  width = 60,
  height = 16,
  color = '#9ca3af', // neutral grey-400
}: SparklineProps) {
  if (!points || points.length === 0) {
    return <span className="text-xs text-gray-300">—</span>;
  }
  // Normalize to numbers
  const data = points.map((p) => ({ day: p.day, cost: Number(p.cost_usd) }));
  return (
    <div style={{ width, height }} aria-label="7-day cost trend" role="img">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <YAxis hide domain={[0, 'dataMax']} />
          <Line
            type="monotone"
            dataKey="cost"
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
