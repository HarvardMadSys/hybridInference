'use client';

import { useState } from 'react';
import toast from 'react-hot-toast';
import { getUserAutomationScore } from '@/lib/api/admin';
import type { UserAutomationScore } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { bandStyle, DETAIL_LABELS, SIGNAL_LABELS } from './lib/automation';

function fmtMetric(value: number | null): string {
  if (value == null) return '—';
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}

function ScoreCard({ score }: { score: UserAutomationScore }) {
  const band = bandStyle(score.band);
  return (
    <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className={`text-[24px] font-bold tabular-nums ${band.text}`}>
          {score.score.toFixed(2)}
        </div>
        <span
          className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${band.chip}`}
        >
          {band.label}
        </span>
        <div className="text-[11px] text-gray-400">
          confidence {score.confidence.toFixed(2)} · {score.n_req.toLocaleString()} req
          {score.insufficient_data && (
            <span className="ml-1 text-amber-600">· insufficient data</span>
          )}
        </div>
      </div>

      {/* Per-signal sub-scores (HIGH bar = more script-like). */}
      <div className="space-y-1">
        {Object.entries(SIGNAL_LABELS).map(([key, label]) => {
          const sig = score.signals[key];
          const sub = sig?.sub;
          return (
            <div key={key} className="flex items-center gap-2 text-[11px]">
              <div className="w-28 shrink-0 text-gray-500">{label}</div>
              {sig?.available && sub != null ? (
                <>
                  <div className="h-2 flex-1 overflow-hidden rounded bg-gray-100">
                    <div
                      className="h-full bg-gray-400"
                      style={{ width: `${Math.round(sub * 100)}%` }}
                    />
                  </div>
                  <div className="w-9 text-right tabular-nums text-gray-600">{sub.toFixed(2)}</div>
                </>
              ) : (
                <div className="flex-1 text-gray-300">— not enough data</div>
              )}
            </div>
          );
        })}
      </div>

      {/* Supporting metrics behind the sub-scores. */}
      {Object.keys(score.detail).length > 0 && (
        <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 border-t border-gray-100 pt-2 text-[11px] sm:grid-cols-3">
          {Object.entries(DETAIL_LABELS).map(([key, label]) =>
            score.detail[key] != null ? (
              <div key={key} className="flex justify-between gap-2">
                <span className="text-gray-400">{label}</span>
                <span className="tabular-nums text-gray-600">{fmtMetric(score.detail[key])}</span>
              </div>
            ) : null,
          )}
        </div>
      )}

      <p className="text-[10px] leading-snug text-gray-400">
        HIGH = mostly automatic scripts/batch/cron · LOW = interactive human (incl. human-driven
        coding agents). A heuristic for triage — read it together with the confidence and the
        per-signal bars.
      </p>
    </div>
  );
}

/**
 * On-demand automation score for a single user, shown inside the admin user
 * detail panel. The score is computed only when the admin clicks the button
 * (it is comparatively expensive), mirroring the on-demand UserRecentRequests.
 */
export function UserAutomationPanel({ userId }: { userId: string }) {
  const [score, setScore] = useState<UserAutomationScore | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      setScore(await getUserAutomationScore(userId));
    } catch (e) {
      const msg = getErrorMessage(e);
      setError(msg);
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-2 border-t border-gray-200 pt-4">
      <div className="flex items-center justify-between">
        <div className="text-[11px] font-medium uppercase tracking-wide text-gray-500">
          Automation score
          <span className="ml-1 normal-case text-gray-400">— human vs. script (last 30d)</span>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={loading}
          className="rounded-md border border-gray-200 bg-white px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50"
        >
          {loading ? 'Computing…' : score ? 'Recompute' : 'Compute automation score'}
        </button>
      </div>
      {error && <div className="text-[12px] text-red-500">Failed to compute: {error}</div>}
      {score && <ScoreCard score={score} />}
    </div>
  );
}
