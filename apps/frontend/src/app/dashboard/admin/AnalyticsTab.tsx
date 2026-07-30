'use client';

import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { AdminAnalyticsResponse, AnalyticsPeriod, getAnalytics } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

// The chart cards carry the recharts bundle; loading them dynamically lets the
// analytics fetch start while that chunk is still downloading.
const AnalyticsCards = dynamic(() => import('./AnalyticsCards'), {
  ssr: false,
  loading: () => <AnalyticsSkeleton />,
});

const PERIODS: { key: AnalyticsPeriod; label: string }[] = [
  { key: 'hour', label: 'Hour' },
  { key: 'day', label: 'Day' },
  { key: 'week', label: 'Week' },
  { key: 'month', label: 'Month' },
];

function SkeletonCard({ className = '' }: { className?: string }) {
  return (
    <div className={`rounded-xl border border-gray-100 bg-gray-50 p-5 ${className}`}>
      <div className="mb-3 h-2.5 w-24 animate-pulse rounded bg-gray-200" />
      <div className="h-8 w-16 animate-pulse rounded bg-gray-200" />
      <div className="mt-4 h-10 w-full animate-pulse rounded bg-gray-200" />
    </div>
  );
}

function AnalyticsSkeleton() {
  return (
    <>
      <SkeletonCard />
      <SkeletonCard />
      <SkeletonCard />
      <SkeletonCard />
      <SkeletonCard className="sm:col-span-2" />
      <SkeletonCard className="sm:col-span-2" />
    </>
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
        <span
          aria-current="page"
          className="bg-gray-900 px-3.5 py-1.5 text-[13px] font-medium text-white"
        >
          Overview
        </span>
        {/* prefetch off: the geo page carries the d3/topojson bundle. */}
        <Link
          className="px-3.5 py-1.5 text-[13px] font-medium text-gray-600 transition hover:bg-gray-50"
          href="/dashboard/admin/analytics/geo"
          prefetch={false}
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
        {loading || !data ? <AnalyticsSkeleton /> : <AnalyticsCards data={data} period={period} />}
      </div>
    </div>
  );
}
