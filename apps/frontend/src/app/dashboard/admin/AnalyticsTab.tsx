'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { AdminAnalyticsResponse, AnalyticsPeriod, getAnalytics } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const PERIODS: { key: AnalyticsPeriod; label: string }[] = [
  { key: 'hour', label: 'Hour' },
  { key: 'day', label: 'Day' },
  { key: 'week', label: 'Week' },
  { key: 'month', label: 'Month' },
];

const CHART_COLORS = [
  '#3b82f6',
  '#f59e0b',
  '#10b981',
  '#8b5cf6',
  '#ec4899',
  '#06b6d4',
  '#f97316',
  '#84cc16',
  '#e11d48',
  '#7c3aed',
];
const OTHERS_COLOR = '#9ca3af';

const numberFmt = new Intl.NumberFormat('en-US');

function fmtCount(n: number): string {
  return numberFmt.format(n);
}

function colorFor(name: string, index: number): string {
  if (name === 'others' || name === 'unknown') return OTHERS_COLOR;
  return CHART_COLORS[index % CHART_COLORS.length];
}

function pct(fraction: number): string {
  return `${(fraction * 100).toFixed(1)}%`;
}

function fmtBucketTime(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

const TOOLTIP_STYLE = {
  fontSize: 12,
  borderRadius: 8,
  border: '1px solid #e5e7eb',
  boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
} as const;

function CardTitle({
  children,
  className = '',
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <p className={`text-xs font-semibold uppercase tracking-wide text-gray-500 ${className}`}>
      {children}
    </p>
  );
}

function ActiveUsersCard({
  data,
  period,
}: {
  data: AdminAnalyticsResponse;
  period: AnalyticsPeriod;
}) {
  const sparklineData = data.sparkline.map((b) => ({
    t: b.start_time,
    v: b.request_count,
  }));
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <CardTitle>Active Users</CardTitle>
      <p className="mt-1.5 text-[40px] font-bold leading-none text-gray-900">{data.active_users}</p>
      <p className="mt-1.5 text-xs text-gray-500">unique users · past {period}</p>
      <div className="mt-4 h-12">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={sparklineData} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
            <XAxis dataKey="t" hide />
            <Tooltip
              cursor={{ fill: 'rgba(59,130,246,0.08)' }}
              separator=""
              formatter={(value) => [`${fmtCount(value as number)} requests`, '']}
              labelFormatter={(label) => fmtBucketTime(label as string)}
              contentStyle={TOOLTIP_STYLE}
            />
            <Bar dataKey="v" fill="#60a5fa" radius={[2, 2, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1.5 text-[11px] text-gray-400">total requests over time</p>
    </div>
  );
}

function avgTurns(value: number | null): string {
  return value == null ? '—' : value.toFixed(1);
}

function ConversationDepthCard({
  data,
  period,
}: {
  data: AdminAnalyticsResponse;
  period: AnalyticsPeriod;
}) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <CardTitle>Avg Conversation Depth</CardTitle>
      <div className="mt-1.5 flex gap-8">
        <div>
          <p className="text-[40px] font-bold leading-none text-gray-900">
            {avgTurns(data.avg_turns)}
          </p>
          <p className="mt-1.5 text-xs text-gray-500">turns / request</p>
        </div>
        <div>
          <p className="text-[40px] font-bold leading-none text-gray-900">
            {avgTurns(data.avg_user_turns)}
          </p>
          <p className="mt-1.5 text-xs text-gray-500">user turns / request</p>
        </div>
      </div>
      <p className="mt-4 text-[11px] text-gray-400">
        mean messages per chat request · past {period}
      </p>
    </div>
  );
}

function DonutCard({
  title,
  entries,
}: {
  title: string;
  entries: AdminAnalyticsResponse['by_model'];
}) {
  const data = entries.map((e, i) => ({
    name: e.name,
    value: e.requests,
    fraction: e.fraction,
    color: colorFor(e.name, i),
  }));
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <CardTitle className="mb-4">{title}</CardTitle>
      {data.length === 0 ? (
        <p className="text-sm text-gray-400">No data</p>
      ) : (
        <div className="flex items-center gap-5">
          <div className="h-[104px] w-[104px] shrink-0">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={data}
                  cx="50%"
                  cy="50%"
                  innerRadius={32}
                  outerRadius={50}
                  dataKey="value"
                  isAnimationActive={false}
                  stroke="none"
                >
                  {data.map((entry, i) => (
                    <Cell key={i} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value, name) => [`${fmtCount(value as number)} requests`, name]}
                  contentStyle={TOOLTIP_STYLE}
                />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            {data.map((entry, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-sm"
                  style={{ background: entry.color }}
                />
                <span className="min-w-0 flex-1 truncate text-gray-700" title={entry.name}>
                  {entry.name}
                </span>
                <span className="shrink-0 tabular-nums text-gray-400">{fmtCount(entry.value)}</span>
                <span className="w-12 shrink-0 text-right font-medium tabular-nums text-gray-700">
                  {pct(entry.fraction)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TopUsersCard({ entries }: { entries: AdminAnalyticsResponse['top_users'] }) {
  const chartData = entries.map((e) => {
    const email = e.email || '';
    return {
      email: email.length > 32 ? `${email.slice(0, 30)}…` : email,
      fullEmail: email,
      requests: e.requests,
      pct: parseFloat((e.fraction * 100).toFixed(1)),
    };
  });
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 sm:col-span-2">
      <CardTitle className="mb-4">Top Users by Requests</CardTitle>
      {entries.length === 0 ? (
        <p className="text-sm text-gray-400">No data</p>
      ) : (
        <div style={{ height: Math.max(160, entries.length * 30 + 16) }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              layout="vertical"
              data={chartData}
              margin={{ top: 0, right: 52, left: 0, bottom: 0 }}
              barCategoryGap="20%"
            >
              <XAxis type="number" hide />
              <YAxis
                type="category"
                dataKey="email"
                width={190}
                tick={{ fontSize: 12, fill: '#4b5563' }}
              />
              <Tooltip
                cursor={{ fill: 'rgba(59,130,246,0.06)' }}
                formatter={(v, _n, item) => [
                  `${fmtCount(v as number)} requests · ${item?.payload?.pct ?? 0}%`,
                  item?.payload?.fullEmail ?? 'User',
                ]}
                contentStyle={TOOLTIP_STYLE}
              />
              <Bar dataKey="requests" radius={[0, 3, 3, 0]} isAnimationActive={false}>
                {entries.map((_, i) => (
                  <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                ))}
                <LabelList
                  dataKey="requests"
                  position="right"
                  fill="#374151"
                  fontSize={11}
                  formatter={(v) => (v == null ? '' : fmtCount(Number(v)))}
                />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

function SkeletonCard({ className = '' }: { className?: string }) {
  return (
    <div className={`rounded-xl border border-gray-100 bg-gray-50 p-5 ${className}`}>
      <div className="mb-3 h-2.5 w-24 animate-pulse rounded bg-gray-200" />
      <div className="h-8 w-16 animate-pulse rounded bg-gray-200" />
      <div className="mt-4 h-10 w-full animate-pulse rounded bg-gray-200" />
    </div>
  );
}

export function AnalyticsTab() {
  const [period, setPeriod] = useState<AnalyticsPeriod>('day');
  const [data, setData] = useState<AdminAnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (p: AnalyticsPeriod) => {
    setLoading(true);
    setError(null);
    try {
      const d = await getAnalytics(p);
      setData(d);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(period);
  }, [load, period]);

  const handlePeriod = (p: AnalyticsPeriod) => {
    setPeriod(p);
  };

  return (
    <div className="mt-6">
      <div
        aria-label="Analytics section"
        className="mb-4 inline-flex overflow-hidden rounded-lg border border-gray-200 bg-white"
        role="group"
      >
        <span aria-current="page" className="bg-gray-900 px-3.5 py-1.5 text-[13px] font-medium text-white">
          Overview
        </span>
        <Link
          className="px-3.5 py-1.5 text-[13px] font-medium text-gray-600 transition hover:bg-gray-50"
          href="/dashboard/admin/analytics/geo"
        >
          Request origins
        </Link>
      </div>

      <div className="mb-5 flex gap-2">
        {PERIODS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => handlePeriod(key)}
            className={`rounded-md px-3.5 py-1.5 text-[13px] font-medium transition ${
              period === key
                ? 'bg-gray-900 text-white'
                : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {error && (
        <div className="mb-4 rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-600">{error}</div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {loading || !data ? (
          <>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard className="sm:col-span-2" />
          </>
        ) : (
          <>
            <ActiveUsersCard data={data} period={period} />
            <ConversationDepthCard data={data} period={period} />
            <DonutCard title="Requests by Model" entries={data.by_model} />
            <DonutCard title="Requests by Provider" entries={data.by_provider} />
            <TopUsersCard entries={data.top_users} />
          </>
        )}
      </div>
    </div>
  );
}
