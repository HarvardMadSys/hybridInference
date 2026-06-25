'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import {
  analyzeUsageInsights,
  getUsageInsightsSettings,
  UsageInsightsResponse,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  UsageInsightsReport,
  UsageInsightsReportSkeleton,
} from '@/components/features/admin/UsageInsightsReport';

const DEFAULT_LIMIT = 40;
const MIN_LIMIT = 1;
const MAX_LIMIT = 200;

// Parse the free-form limit input into a valid sample size, falling back to the
// default when it's blank or non-numeric.
function clampLimit(raw: string): number {
  const n = parseInt(raw, 10);
  if (Number.isNaN(n)) return DEFAULT_LIMIT;
  return Math.max(MIN_LIMIT, Math.min(MAX_LIMIT, n));
}

export function UsageInsightsTab() {
  const [userEmail, setUserEmail] = useState('');
  // Kept as a string so the field can be cleared / edited freely; clamped on
  // blur and when the request is built.
  const [limit, setLimit] = useState(String(DEFAULT_LIMIT));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UsageInsightsResponse | null>(null);
  // null until the provider config is fetched; drives the "configure key" notice.
  const [configured, setConfigured] = useState<boolean | null>(null);

  useEffect(() => {
    getUsageInsightsSettings()
      .then((s) => setConfigured(s.configured))
      .catch(() => setConfigured(null));
  }, []);

  const analyze = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await analyzeUsageInsights({
        user_email: userEmail.trim() || undefined,
        limit: clampLimit(limit),
      });
      setResult(res);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [userEmail, limit]);

  return (
    <div className="mt-6 space-y-6">
      <div className="rounded-xl border border-gray-200 bg-white p-5">
        <h2 className="text-[15px] font-semibold text-gray-900">Analyze usage</h2>
        <p className="mt-1 text-[13px] text-gray-500">
          Samples recent request payloads and uses an LLM (via freeinference.org) to summarize{' '}
          <em>how</em> people use the gateway — which agents/harnesses, what tasks, and notable
          patterns. The API key and model are configured in{' '}
          <Link
            href="/dashboard/admin/settings"
            className="font-medium text-gray-900 underline decoration-gray-300 underline-offset-2 hover:decoration-gray-900"
          >
            Settings
          </Link>
          .
        </p>

        {configured === false && (
          <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-[13px] text-amber-800">
            No analysis API key configured. Add a freeinference.org key in{' '}
            <Link href="/dashboard/admin/settings" className="font-semibold underline">
              Settings → Usage Insights
            </Link>{' '}
            first.
          </div>
        )}

        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-[12px] font-medium text-gray-600">Sample size</label>
            <input
              type="number"
              min={MIN_LIMIT}
              max={MAX_LIMIT}
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
              onBlur={() => setLimit(String(clampLimit(limit)))}
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            />
          </div>

          <div>
            <label className="mb-1 block text-[12px] font-medium text-gray-600">
              Scope to a user (optional)
            </label>
            <input
              type="email"
              value={userEmail}
              onChange={(e) => setUserEmail(e.target.value)}
              placeholder="leave blank to analyze all users"
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            />
          </div>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={analyze}
            disabled={loading}
            className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? 'Analyzing…' : 'Analyze usage'}
          </button>
          {result && !loading && (
            <span className="text-[12px] text-gray-400">
              {result.sampled_requests} request(s) · {result.model} · {result.scope}
            </span>
          )}
        </div>

        {error && (
          <div className="mt-4 rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-600">{error}</div>
        )}
      </div>

      {loading && <UsageInsightsReportSkeleton />}

      {result && !loading && <UsageInsightsReport result={result} />}
    </div>
  );
}
