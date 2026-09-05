'use client';

import { useCallback, useState } from 'react';
import {
  analyzeUsageInsights,
  sampleUsageInsights,
  UsageInsightsResponse,
  UsageInsightsSamplesResponse,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  UsageInsightsReport,
  UsageInsightsReportSkeleton,
  UsageInsightsSampleList,
} from '@/components/features/admin/UsageInsightsReport';

const SAMPLE_LIMIT = 100;

/**
 * On-demand LLM analysis of a single user's recent requests, shown in the admin
 * user detail panel. Uses the analysis provider configured in Admin →
 * Settings → Usage Insights; if no key is set the backend returns a 400 that
 * points the admin there. "View user turns" returns the same random sample
 * without calling the model.
 */
export function UserUsageInsights({ userId }: { userId: string }) {
  const [analyzing, setAnalyzing] = useState(false);
  const [sampling, setSampling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UsageInsightsResponse | null>(null);
  const [samples, setSamples] = useState<UsageInsightsSamplesResponse | null>(null);

  const analyze = useCallback(async () => {
    setAnalyzing(true);
    setError(null);
    try {
      const res = await analyzeUsageInsights({ user_id: userId, limit: SAMPLE_LIMIT });
      setResult(res);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setAnalyzing(false);
    }
  }, [userId]);

  const viewTurns = useCallback(async () => {
    setSampling(true);
    setError(null);
    try {
      const res = await sampleUsageInsights({ user_id: userId, limit: SAMPLE_LIMIT });
      setSamples(res);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setSampling(false);
    }
  }, [userId]);

  const busy = analyzing || sampling;

  return (
    <div className="space-y-3 border-t border-gray-200 pt-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[11px] font-medium uppercase tracking-wide text-gray-500">
          Usage insights
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={viewTurns}
            disabled={busy}
            className="rounded-md border border-gray-200 bg-white px-3 py-1.5 text-[12px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {sampling ? 'Loading turns…' : samples ? 'Refresh turns' : 'View user turns'}
          </button>
          <button
            type="button"
            onClick={analyze}
            disabled={busy}
            className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {analyzing ? 'Analyzing…' : result ? 'Re-analyze' : 'Analyze usage'}
          </button>
        </div>
      </div>

      <p className="text-[12px] text-gray-500">
        Random sample of up to {SAMPLE_LIMIT} of this user&apos;s requests. Analyze usage sends each
        request&apos;s user-turn text (not assistant/tool turns) to an LLM; View user turns shows
        those same turns without calling the model.
      </p>

      {error && (
        <div className="rounded-lg bg-red-50 px-4 py-2.5 text-[13px] text-red-600">{error}</div>
      )}

      {sampling && <UsageInsightsReportSkeleton />}
      {samples && !sampling && <UsageInsightsSampleList result={samples} />}

      {analyzing && <UsageInsightsReportSkeleton />}
      {result && !analyzing && <UsageInsightsReport result={result} />}
    </div>
  );
}
