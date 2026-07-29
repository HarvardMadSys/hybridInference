'use client';

import { useCallback, useState } from 'react';
import { analyzeUsageInsights, UsageInsightsResponse } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  UsageInsightsReport,
  UsageInsightsReportSkeleton,
} from '@/components/features/admin/UsageInsightsReport';

const SAMPLE_LIMIT = 100;

/**
 * On-demand LLM analysis of a single user's recent requests, shown in the admin
 * user detail panel. Uses the analysis provider configured in Admin →
 * Settings → Usage Insights; if no key is set the backend returns a 400 that
 * points the admin there.
 */
export function UserUsageInsights({ userId }: { userId: string }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UsageInsightsResponse | null>(null);

  const analyze = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await analyzeUsageInsights({ user_id: userId, limit: SAMPLE_LIMIT });
      setResult(res);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [userId]);

  return (
    <div className="space-y-3 border-t border-gray-200 pt-4">
      <div className="flex items-center justify-between">
        <div className="text-[11px] font-medium uppercase tracking-wide text-gray-500">
          Usage insights
        </div>
        <button
          type="button"
          onClick={analyze}
          disabled={loading}
          className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? 'Analyzing…' : result ? 'Re-analyze' : 'Analyze usage'}
        </button>
      </div>

      <p className="text-[12px] text-gray-500">
        Summarize how this user uses the gateway (which harness, what tasks) from a random sample of
        up to {SAMPLE_LIMIT} of their requests, via an LLM.
      </p>

      {error && (
        <div className="rounded-lg bg-red-50 px-4 py-2.5 text-[13px] text-red-600">{error}</div>
      )}

      {loading && <UsageInsightsReportSkeleton />}

      {result && !loading && <UsageInsightsReport result={result} />}
    </div>
  );
}
