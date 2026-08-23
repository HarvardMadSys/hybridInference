'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AdminRequestPerfDistribution,
  AdminRequestPerfGroup,
  getRecentRequestsPerformance,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

type MetricKind = 'ms' | 'tps';

function formatLatency(ms?: number | null): string {
  if (ms == null) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

function formatThroughput(tps?: number | null): string {
  if (tps == null) return '—';
  if (tps >= 1000) {
    const k = tps / 1000;
    return `${k.toFixed(tps % 1000 === 0 ? 0 : 1)}k`;
  }
  if (tps >= 100) return `${Math.round(tps)}`;
  return tps.toFixed(1);
}

function formatMetric(value: number | null | undefined, kind: MetricKind): string {
  return kind === 'ms' ? formatLatency(value) : formatThroughput(value);
}

/** Mean / median / P10 / P90 cells for one metric of one served route. */
function MetricCells({ dist, kind }: { dist: AdminRequestPerfDistribution; kind: MetricKind }) {
  // A group can have traffic but no measurable samples (e.g. every response
  // decoded in under the throughput floor), so render the whole metric as
  // undefined rather than four zeros.
  const values: Array<number | null> =
    dist.count > 0 ? [dist.mean, dist.p50, dist.p10, dist.p90] : [null, null, null, null];
  return (
    <>
      {values.map((value, index) => (
        <td
          key={index}
          className={`whitespace-nowrap px-2 py-2 text-right text-[11px] tabular-nums text-gray-700 ${
            index === 0 ? 'border-l border-gray-100' : ''
          }`}
        >
          {formatMetric(value, kind)}
        </td>
      ))}
    </>
  );
}

function sampleSummary(group: AdminRequestPerfGroup): string {
  return (
    `${group.request_count.toLocaleString()} successful streaming request(s); ` +
    `TTFT measured on ${group.ttft_ms.count.toLocaleString()}, ` +
    `decode throughput on ${group.decode_throughput_tps.count.toLocaleString()}`
  );
}

/**
 * Per-(model, endpoint) TTFT and decode-throughput summary for the Recent
 * Requests tab.
 *
 * Follows the tab's user / model / type / lookback filters so the summary
 * describes the rows below it. "Errors only" is not applied: the backend always
 * scopes this view to successful streaming requests, since a failed or
 * non-streamed request has no meaningful first-token or decode timing.
 */
export function RequestPerformancePanel({
  days,
  userFilter,
  modelFilter,
  requestType,
  refreshKey = 0,
  errorsOnly = false,
}: {
  days: number;
  userFilter: string;
  modelFilter: string;
  requestType: 'all' | 'chat' | 'embedding';
  refreshKey?: number;
  errorsOnly?: boolean;
}) {
  const [groups, setGroups] = useState<AdminRequestPerfGroup[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Monotonic id so a slow response issued under older filters can't overwrite
  // a newer one (the filters change as the admin types).
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    try {
      const data = await getRecentRequestsPerformance({
        days,
        userId: userFilter || undefined,
        modelId: modelFilter || undefined,
        requestType: requestType === 'all' ? undefined : requestType,
      });
      if (seq !== seqRef.current) return;
      setGroups(data.groups);
      setTruncated(data.truncated);
      setError(null);
    } catch (e) {
      // Reported inline rather than as a toast: this panel refetches on every
      // filter change, and a repeated toast per keystroke would bury the list's
      // own errors.
      if (seq === seqRef.current) {
        setError(getErrorMessage(e));
        setGroups([]);
        setTruncated(false);
      }
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [days, userFilter, modelFilter, requestType]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  return (
    <div className="mt-4 rounded-2xl border border-gray-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-100 px-4 py-3">
        <div>
          <h2 className="text-[14px] font-semibold text-gray-900">
            Per-endpoint TTFT &amp; decode throughput
          </h2>
          <p className="text-[11px] text-gray-400">
            Successful streaming requests over the last {days}d, split by the model and endpoint
            that served them
            {errorsOnly ? ' (not narrowed by “errors only”)' : ''}.
          </p>
        </div>
        {loading && (
          <span className="mt-1 h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        )}
      </div>

      {error ? (
        <p className="px-4 py-6 text-center text-[13px] text-red-600">
          Failed to load per-endpoint performance: {error}
        </p>
      ) : groups.length === 0 ? (
        !loading && (
          <p className="px-4 py-6 text-center text-[13px] text-gray-400">
            No streaming requests matched these filters.
          </p>
        )
      ) : (
        <>
          <div className="max-h-[22rem] overflow-auto">
            <table className="min-w-full">
              <thead className="sticky top-0 z-10 bg-gray-50/95 backdrop-blur">
                <tr className="border-b border-gray-200">
                  <th
                    rowSpan={2}
                    className="py-2 pl-4 pr-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500"
                  >
                    Model
                  </th>
                  <th
                    rowSpan={2}
                    className="px-2 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500"
                  >
                    Endpoint
                  </th>
                  <th
                    rowSpan={2}
                    className="px-2 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-gray-500"
                  >
                    Reqs
                  </th>
                  <th
                    colSpan={4}
                    className="border-l border-gray-200 px-2 pt-2 text-center text-[11px] font-medium uppercase tracking-wider text-gray-500"
                  >
                    TTFT
                  </th>
                  <th
                    colSpan={4}
                    className="border-l border-gray-200 px-2 pt-2 text-center text-[11px] font-medium uppercase tracking-wider text-gray-500"
                  >
                    Decode (tok/s)
                  </th>
                </tr>
                <tr className="border-b border-gray-200">
                  {(['ttft', 'decode'] as const).map((metric) =>
                    ['Mean', 'Median', 'P10', 'P90'].map((label, index) => (
                      <th
                        key={`${metric}-${label}`}
                        className={`px-2 pb-2 text-right text-[10px] font-medium uppercase tracking-wider text-gray-400 ${
                          index === 0 ? 'border-l border-gray-200' : ''
                        }`}
                      >
                        {label}
                      </th>
                    )),
                  )}
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => (
                  <tr
                    key={`${group.model_id}|${group.endpoint_id}`}
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
                    <td
                      className="whitespace-nowrap px-2 py-2 text-right text-[11px] tabular-nums text-gray-700"
                      title={sampleSummary(group)}
                    >
                      {group.request_count.toLocaleString()}
                    </td>
                    <MetricCells dist={group.ttft_ms} kind="ms" />
                    <MetricCells dist={group.decode_throughput_tps} kind="tps" />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {truncated && (
            <p className="border-t border-gray-100 px-4 py-2 text-[11px] text-gray-400">
              Showing the {groups.length.toLocaleString()} busiest model/endpoint pairs; quieter
              ones are omitted.
            </p>
          )}
        </>
      )}
    </div>
  );
}
