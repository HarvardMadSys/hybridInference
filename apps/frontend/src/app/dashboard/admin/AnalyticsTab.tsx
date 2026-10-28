'use client';

import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import {
  AdminAnalyticsResponse,
  AnalyticsPeriod,
  TokenUsageRange,
  getAnalytics,
  listRuntimeSettings,
} from '@/lib/api/admin';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
} from '@/components/features/admin/AdminPrimitives';
import { BUTTON_SECONDARY } from '@/components/features/admin/adminStyles';
import { getErrorMessage } from '@/lib/utils/errors';
import { PerformanceMetricsSection, TokenUsageSection } from './TokenUsageSection';

// The chart cards carry the recharts bundle; loading them dynamically lets the
// analytics fetch start while that chunk is still downloading.
const AnalyticsCards = dynamic(() => import('./AnalyticsCards'), {
  ssr: false,
  loading: () => <AnalyticsSkeleton />,
});

const GrowthCard = dynamic(() => import('./GrowthCard'), {
  ssr: false,
  loading: () => <SkeletonCard className="sm:col-span-2" />,
});

const PERIODS: { key: AnalyticsPeriod; label: string }[] = [
  { key: 'hour', label: 'Hour' },
  { key: 'day', label: 'Day' },
  { key: 'week', label: 'Week' },
  { key: 'month', label: 'Month' },
];

// The period buttons scope token usage and users/requests. Token usage covers the same
// lookback, in the whole hours its hourly rollup is kept in.
const TOKEN_USAGE_RANGE: Record<AnalyticsPeriod, TokenUsageRange> = {
  hour: '1h',
  day: '24h',
  week: '7d',
  month: '30d',
};

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

function UsersAndRequestsSection({ period }: { period: AnalyticsPeriod }) {
  const [data, setData] = useState<AdminAnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void getAnalytics(period)
      .then((response) => {
        if (!cancelled) setData(response);
      })
      .catch((reason) => {
        if (!cancelled) setError(getErrorMessage(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [period]);

  return (
    <section>
      <div className="mb-4">
        <h2 className="text-[14px] font-semibold text-gray-900">Users and requests</h2>
        <p className="text-[11px] text-gray-400">
          Who sends requests, and which models and providers serve them.
        </p>
      </div>
      {error && (
        <div className="mb-4 rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-600">{error}</div>
      )}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {loading || !data ? <AnalyticsSkeleton /> : <AnalyticsCards data={data} period={period} />}
      </div>
    </section>
  );
}

interface EnabledSections {
  tokenUsage: boolean;
  usersAndRequests: boolean;
  performanceMetrics: boolean;
  growthRate: boolean;
}

export function AnalyticsTab() {
  const [period, setPeriod] = useState<AnalyticsPeriod>('day');
  const [sections, setSections] = useState<EnabledSections | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setSections(null);
    setSettingsError(null);
    void listRuntimeSettings()
      .then(({ settings }) => {
        if (cancelled) return;
        const enabled = (key: string, fallback: boolean) => {
          const setting = settings.find((item) => item.key === key);
          return setting ? setting.value === true : fallback;
        };
        setSections({
          tokenUsage: enabled('analytics_token_usage_enabled', true),
          usersAndRequests: enabled('analytics_users_and_requests_enabled', true),
          performanceMetrics: enabled('analytics_performance_metrics_enabled', true),
          growthRate: enabled('analytics_growth_rate_enabled', false),
        });
      })
      .catch((reason) => {
        if (!cancelled) setSettingsError(getErrorMessage(reason));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  if (settingsError) {
    return (
      <AdminNotice
        role="alert"
        action={
          <button
            type="button"
            className={BUTTON_SECONDARY}
            onClick={() => setAttempt((value) => value + 1)}
          >
            Retry
          </button>
        }
      >
        Could not load Analytics settings: {settingsError}
      </AdminNotice>
    );
  }
  if (!sections) return <AdminLoading label="Loading Analytics settings…" />;
  if (!Object.values(sections).some(Boolean)) {
    return (
      <AdminEmptyState title="All Analytics overview sections are disabled.">
        <Link
          href="/dashboard/admin/settings?tab=common#settings-analytics"
          className="font-medium text-gray-700 underline hover:text-gray-900"
        >
          Open Settings to enable a section
        </Link>
      </AdminEmptyState>
    );
  }

  return (
    <div>
      {(sections.tokenUsage || sections.usersAndRequests) && (
        <div className="mb-5 flex gap-2">
          {PERIODS.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setPeriod(key)}
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
      )}
      <div className="space-y-8">
        {sections.tokenUsage && <TokenUsageSection range={TOKEN_USAGE_RANGE[period]} />}
        {sections.usersAndRequests && <UsersAndRequestsSection period={period} />}
        {sections.performanceMetrics && <PerformanceMetricsSection />}
        {sections.growthRate && <GrowthCard />}
      </div>
    </div>
  );
}
