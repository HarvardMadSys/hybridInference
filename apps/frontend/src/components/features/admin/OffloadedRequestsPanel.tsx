'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AdminRequestOffloadGroup,
  type RequestOutcome,
  getRecentRequestOffloads,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { routeKeyOf } from './RequestPerformancePanel';

/** The window this table counts over: the past day, whatever the tab's lookback. */
const WINDOW_DAYS = 1;
/** How that window is written in this panel's copy. */
const WINDOW_LABEL = '24h';

/** The reasons the router records on an offloaded request, in column order. */
const REASON_COLUMNS = [
  {
    reason: 'queue_wait',
    label: 'Queue wait',
    title: 'An earlier attempt waited out the wait for an outbound slot in the gateway',
  },
  {
    reason: 'engine_wait',
    label: 'Engine wait',
    title: "An earlier attempt's engine sent no first token within the wait",
  },
  {
    reason: 'last_resort',
    label: 'Last resort',
    title: 'No other route of the model could serve the request',
  },
] as const;
const KNOWN_REASONS: ReadonlySet<string> = new Set(REASON_COLUMNS.map((column) => column.reason));

/** Offloaded requests in a group whose reason has no column of its own. */
function otherCount(group: AdminRequestOffloadGroup): number {
  return Object.entries(group.reasons).reduce(
    (sum, [reason, count]) => (KNOWN_REASONS.has(reason) ? sum : sum + count),
    0,
  );
}

function formatShare(count: number, total: number): string {
  if (total <= 0) return '—';
  const pct = (count / total) * 100;
  if (pct < 0.1) return '<0.1%';
  return `${pct.toFixed(pct >= 10 ? 0 : 1)}%`;
}

function CountCell({ value, emphasis = false }: { value: number; emphasis?: boolean }) {
  return (
    <td
      className={`whitespace-nowrap px-2 py-2 text-right text-[11px] tabular-nums ${
        value === 0 ? 'text-gray-300' : emphasis ? 'font-medium text-gray-900' : 'text-gray-700'
      }`}
    >
      {value === 0 ? '—' : value.toLocaleString()}
    </td>
  );
}

/**
 * How many requests each model sent to its offload route over the past day,
 * for the Recent Requests tab.
 *
 * One row per model and the offload route its requests were sent to, split by
 * why the router offloaded them, with the share of the model's traffic that
 * was offloaded. Follows the tab's user / session / model / type filters, like
 * the performance panel beside it, but always counts over the last
 * `WINDOW_LABEL`, and counts every outcome: a request sent to the offload route
 * was offloaded whatever happened next, and the Failed column says how many
 * failed anyway.
 */
export function OffloadedRequestsPanel({
  userFilter,
  sessionFilter,
  modelFilter,
  requestType,
  refreshKey = 0,
  outcome = 'all',
}: {
  userFilter: string;
  sessionFilter: string;
  modelFilter: string;
  requestType: 'all' | 'chat' | 'embedding';
  refreshKey?: number;
  outcome?: RequestOutcome;
}) {
  const [groups, setGroups] = useState<AdminRequestOffloadGroup[]>([]);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Monotonic id so a slow response issued under older filters can't overwrite
  // a newer one (the filters change as the admin types).
  const seqRef = useRef(0);
  // Last refreshKey acted on: only a reload that Refresh asked for bypasses the
  // backend's short-lived per-filter cache, as in the performance panel.
  const refreshKeyRef = useRef(refreshKey);

  const load = useCallback(
    async (refresh: boolean) => {
      const seq = ++seqRef.current;
      setLoading(true);
      try {
        const data = await getRecentRequestOffloads({
          days: WINDOW_DAYS,
          userId: userFilter || undefined,
          sessionId: sessionFilter || undefined,
          modelId: modelFilter || undefined,
          requestType: requestType === 'all' ? undefined : requestType,
          refresh,
        });
        if (seq !== seqRef.current) return;
        setGroups(data.groups);
        setTotal(data.total_offloaded);
        setTruncated(data.truncated);
        setError(null);
      } catch (e) {
        // Inline rather than a toast, as in the performance panel: this refetches
        // on every filter change.
        if (seq === seqRef.current) {
          setError(getErrorMessage(e));
          setGroups([]);
          setTotal(0);
          setTruncated(false);
        }
      } finally {
        if (seq === seqRef.current) setLoading(false);
      }
    },
    [userFilter, sessionFilter, modelFilter, requestType],
  );

  useEffect(() => {
    const isRefresh = refreshKey !== refreshKeyRef.current;
    refreshKeyRef.current = refreshKey;
    load(isRefresh);
  }, [load, refreshKey]);

  const showOther = groups.some((group) => otherCount(group) > 0);

  return (
    <div className="mt-4 rounded-2xl border border-gray-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-100 px-4 py-3">
        <div>
          <h2 className="text-[14px] font-semibold text-gray-900">Offloaded requests</h2>
          <p className="text-[11px] text-gray-400">
            Requests sent to a model&apos;s offload route over the last {WINDOW_LABEL}, by route and
            reason
            {outcome !== 'all' ? ' (not narrowed by the outcome filter)' : ''}.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {loading && (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
          {!error && (
            <span
              className="rounded-full bg-gray-100 px-2 py-0.5 text-[12px] font-medium tabular-nums text-gray-700"
              data-testid="offloaded-total"
            >
              {total.toLocaleString()} offloaded
            </span>
          )}
        </div>
      </div>

      {error ? (
        <p className="px-4 py-6 text-center text-[13px] text-red-600">
          Failed to load offloaded requests: {error}
        </p>
      ) : groups.length === 0 ? (
        !loading && (
          <p className="px-4 py-6 text-center text-[13px] text-gray-400">
            No requests were offloaded in the last {WINDOW_LABEL}.
          </p>
        )
      ) : (
        <>
          <div className="max-h-[22rem] overflow-auto">
            <table className="min-w-full" aria-label="Offloaded requests">
              <thead className="sticky top-0 z-10 bg-gray-50/95 backdrop-blur">
                <tr className="border-b border-gray-200">
                  <th className="py-2 pl-4 pr-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Model
                  </th>
                  <th className="px-2 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Offload route
                  </th>
                  {REASON_COLUMNS.map((column) => (
                    <th
                      key={column.reason}
                      title={column.title}
                      className="px-2 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-gray-500"
                    >
                      {column.label}
                    </th>
                  ))}
                  {showOther && (
                    <th
                      title="A reason this console does not have a column for"
                      className="px-2 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-gray-500"
                    >
                      Other
                    </th>
                  )}
                  <th className="border-l border-gray-200 px-2 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Offloaded
                  </th>
                  <th
                    title="Offloaded requests as a share of all the model's requests in the window"
                    className="px-2 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-gray-500"
                  >
                    Share
                  </th>
                  <th
                    title="Offloaded requests that failed anyway (client disconnects not counted)"
                    className="py-2 pl-2 pr-4 text-right text-[11px] font-medium uppercase tracking-wider text-gray-500"
                  >
                    Failed
                  </th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => (
                  <tr
                    key={routeKeyOf(group)}
                    className="border-b border-gray-100 last:border-b-0 hover:bg-gray-50/60"
                  >
                    <td
                      className="max-w-[180px] truncate py-2 pl-4 pr-2 text-[12px] font-medium text-gray-900"
                      title={group.model_id}
                    >
                      {group.model_id}
                    </td>
                    <td
                      className="max-w-[200px] truncate px-2 py-2 font-mono text-[11px] text-gray-600"
                      title={group.endpoint_id}
                    >
                      {group.endpoint_id}
                    </td>
                    {REASON_COLUMNS.map((column) => (
                      <CountCell key={column.reason} value={group.reasons[column.reason] ?? 0} />
                    ))}
                    {showOther && <CountCell value={otherCount(group)} />}
                    <td className="whitespace-nowrap border-l border-gray-100 px-2 py-2 text-right text-[11px] font-medium tabular-nums text-gray-900">
                      {group.request_count.toLocaleString()}
                    </td>
                    <td
                      className="whitespace-nowrap px-2 py-2 text-right text-[11px] tabular-nums text-gray-700"
                      title={`${group.request_count.toLocaleString()} of ${group.model_request_count.toLocaleString()} requests for ${group.model_id}`}
                    >
                      {formatShare(group.request_count, group.model_request_count)}
                    </td>
                    <td
                      className={`whitespace-nowrap py-2 pl-2 pr-4 text-right text-[11px] tabular-nums ${
                        group.failed_count > 0 ? 'font-medium text-red-600' : 'text-gray-300'
                      }`}
                    >
                      {group.failed_count > 0 ? group.failed_count.toLocaleString() : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {truncated && (
            <p className="border-t border-gray-100 px-4 py-2 text-[11px] text-gray-400">
              Showing the {groups.length.toLocaleString()} routes with the most offloaded requests;
              the total above counts them all.
            </p>
          )}
        </>
      )}
    </div>
  );
}
