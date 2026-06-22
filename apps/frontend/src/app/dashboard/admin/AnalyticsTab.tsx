'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  Bar,
  BarChart,
  Cell,
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

function colorFor(name: string, index: number): string {
  if (name === 'others' || name === 'unknown') return OTHERS_COLOR;
  return CHART_COLORS[index % CHART_COLORS.length];
}

function pct(fraction: number): string {
  return `${(fraction * 100).toFixed(1)}%`;
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
      <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        Active Users
      </p>
      <p className="mt-1 text-[40px] font-bold leading-none text-gray-900">{data.active_users}</p>
      <p className="mt-1 text-[12px] text-gray-400">unique users · past {period}</p>
      <div className="mt-4 h-10">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={sparklineData} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
            <Bar dataKey="v" fill="#93c5fd" radius={[2, 2, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 text-[10px] text-gray-300">total requests over time</p>
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
      <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        Avg Conversation Depth
      </p>
      <div className="mt-1 flex gap-8">
        <div>
          <p className="text-[40px] font-bold leading-none text-gray-900">
            {avgTurns(data.avg_turns)}
          </p>
          <p className="mt-1 text-[12px] text-gray-400">turns / request</p>
        </div>
        <div>
          <p className="text-[40px] font-bold leading-none text-gray-900">
            {avgTurns(data.avg_user_turns)}
          </p>
          <p className="mt-1 text-[12px] text-gray-400">user turns / request</p>
        </div>
      </div>
      <p className="mt-4 text-[10px] text-gray-300">
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
      <p className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        {title}
      </p>
      {data.length === 0 ? (
        <p className="text-[13px] text-gray-300">No data</p>
      ) : (
        <div className="flex items-center gap-4">
          <div className="h-[90px] w-[90px] shrink-0">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={data}
                  cx="50%"
                  cy="50%"
                  innerRadius={28}
                  outerRadius={44}
                  dataKey="value"
                  isAnimationActive={false}
                  stroke="none"
                >
                  {data.map((entry, i) => (
                    <Cell key={i} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value, name) => [value, name]}
                  contentStyle={{ fontSize: 11 }}
                />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="flex flex-col gap-1.5 overflow-hidden">
            {data.map((entry, i) => (
              <div key={i} className="flex items-center gap-1.5 text-[11px]">
                <div
                  className="h-2.5 w-2.5 shrink-0 rounded-sm"
                  style={{ background: entry.color }}
                />
                <span className="truncate text-gray-700">{entry.name}</span>
                <span className="ml-auto shrink-0 text-gray-400">{pct(entry.fraction)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TopUsersCard({ entries }: { entries: AdminAnalyticsResponse['top_users'] }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <p className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        Top Users by Requests
      </p>
      {entries.length === 0 ? (
        <p className="text-[13px] text-gray-300">No data</p>
      ) : (
        <div className="h-[160px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              layout="vertical"
              data={entries.map((e) => ({
                email: e.email.length > 22 ? `${e.email.slice(0, 20)}…` : e.email,
                requests: e.requests,
                pct: parseFloat((e.fraction * 100).toFixed(1)),
              }))}
              margin={{ top: 0, right: 40, left: 0, bottom: 0 }}
            >
              <XAxis type="number" hide />
              <YAxis
                type="category"
                dataKey="email"
                width={110}
                tick={{ fontSize: 10, fill: '#6b7280' }}
              />
              <Tooltip
                formatter={(v) => [`${v} reqs`, 'Requests']}
                contentStyle={{ fontSize: 11 }}
              />
              <Bar
                dataKey="requests"
                fill="#3b82f6"
                radius={[0, 3, 3, 0]}
                isAnimationActive={false}
              >
                {entries.map((_, i) => (
                  <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

function SkeletonCard() {
  return (
    <div className="rounded-xl border border-gray-100 bg-gray-50 p-5">
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
        <div className="mb-4 rounded-lg bg-red-50 px-4 py-2.5 text-[13px] text-red-600">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {loading || !data ? (
          <>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
          </>
        ) : (
          <>
            <ActiveUsersCard data={data} period={period} />
            <ConversationDepthCard data={data} period={period} />
            <DonutCard title="Requests by Model" entries={data.by_model} />
            <TopUsersCard entries={data.top_users} />
            <DonutCard title="Requests by Provider" entries={data.by_provider} />
          </>
        )}
      </div>
    </div>
  );
}
