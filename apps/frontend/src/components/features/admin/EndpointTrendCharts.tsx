'use client';

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { AdminRequestPerfTrendSeries } from '@/lib/api/admin';

// Two series per chart, so identity is never carried by color alone: both charts
// ship a legend, and the tooltip names each value. Blue matches the TTFT chart
// on the Providers tab; this amber is the nearest step to that chart's that
// still clears 3:1 against a white surface (#f59e0b does not).
const P50_COLOR = '#3b82f6';
const P90_COLOR = '#d97706';

type ChartPoint = {
  label: string;
  ttft_p50: number | null;
  ttft_p90: number | null;
  tps_p50: number | null;
  tps_p90: number | null;
  requests: number;
};

/**
 * Label one bucket for the x axis and the tooltip.
 *
 * A clock time alone is only unambiguous while the window is a single day. The
 * 30- and 90-day windows bucket daily, where every label would otherwise read
 * "12:00 AM", and the 7-day window repeats each time four times — so once the
 * window spans days the date goes in, and once buckets are a day wide the time
 * comes out.
 */
export function bucketLabel(iso: string, bucketMinutes: number, days: number): string {
  const at = new Date(iso);
  if (bucketMinutes >= 1440) {
    return at.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (days <= 1) return time;
  return `${at.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

export function buildTrendPoints(
  series: AdminRequestPerfTrendSeries,
  bucketMinutes = 60,
  days = 1,
): ChartPoint[] {
  return series.buckets.map((bucket) => ({
    label: bucketLabel(bucket.start_time, bucketMinutes, days),
    ttft_p50: bucket.ttft_ms_p50,
    ttft_p90: bucket.ttft_ms_p90,
    tps_p50: bucket.decode_throughput_tps_p50,
    tps_p90: bucket.decode_throughput_tps_p90,
    requests: bucket.request_count,
  }));
}

function TrendChart({
  title,
  points,
  p50Key,
  p90Key,
  unit,
  testId,
}: {
  title: string;
  points: ChartPoint[];
  p50Key: 'ttft_p50' | 'tps_p50';
  p90Key: 'ttft_p90' | 'tps_p90';
  unit: string;
  testId: string;
}) {
  const hasSample = points.some((p) => p[p50Key] != null);
  return (
    <div data-testid={testId} className="rounded-lg border border-gray-200 p-3">
      <p className="mb-1 text-[12px] font-semibold text-gray-800">{title}</p>
      {hasSample ? (
        <div className="h-40">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="#f1f5f9" strokeDasharray="3 3" />
              <XAxis dataKey="label" minTickGap={28} tick={{ fontSize: 10 }} stroke="#94a3b8" />
              <YAxis tick={{ fontSize: 10 }} stroke="#94a3b8" width={44} />
              <Tooltip
                formatter={(value, name) => [`${String(value)} ${unit}`, String(name)]}
                labelFormatter={(label) => `Bucket starting ${String(label)}`}
                contentStyle={{ fontSize: 11 }}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {/* connectNulls stays off: a quiet bucket is a gap in the line, not
                  a straight segment drawn across an outage. Small dots keep an
                  isolated bucket visible, which a bare line would render as
                  nothing. */}
              <Line
                type="monotone"
                dataKey={p50Key}
                name="median"
                stroke={P50_COLOR}
                strokeWidth={2}
                dot={{ r: 2.5 }}
                connectNulls={false}
              />
              <Line
                type="monotone"
                dataKey={p90Key}
                name="P90"
                stroke={P90_COLOR}
                strokeWidth={2}
                dot={{ r: 2.5 }}
                connectNulls={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="py-10 text-center text-[12px] text-gray-400">
          No measurable samples in this window.
        </p>
      )}
    </div>
  );
}

/**
 * TTFT and decode-throughput trends for one served route.
 *
 * Two charts rather than one with two y axes: milliseconds and tokens/second
 * share no scale, and overlaying them would invent a relationship between the
 * curves that the numbers do not have.
 */
export function EndpointTrendCharts({
  series,
  bucketMinutes,
  days,
}: {
  series: AdminRequestPerfTrendSeries;
  bucketMinutes: number;
  days: number;
}) {
  const points = buildTrendPoints(series, bucketMinutes, days);
  const measured = points.filter((p) => p.requests > 0).length;

  return (
    <div className="space-y-2">
      <p className="text-[11px] text-gray-500">
        {series.endpoint_id} — {series.request_count.toLocaleString()} requests across {measured} of{' '}
        {points.length} {bucketMinutes === 60 ? 'hourly' : `${bucketMinutes}-minute`} buckets
      </p>
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <TrendChart
          title="TTFT (ms)"
          points={points}
          p50Key="ttft_p50"
          p90Key="ttft_p90"
          unit="ms"
          testId="endpoint-trend-ttft"
        />
        <TrendChart
          title="Decode throughput (tok/s)"
          points={points}
          p50Key="tps_p50"
          p90Key="tps_p90"
          unit="tok/s"
          testId="endpoint-trend-throughput"
        />
      </div>
    </div>
  );
}
