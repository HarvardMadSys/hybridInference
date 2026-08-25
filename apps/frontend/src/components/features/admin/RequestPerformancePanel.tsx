'use client';

import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import {
  AdminRequestPerfDistribution,
  AdminRequestPerfGroup,
  AdminRequestPerfTrendSeries,
  getRecentRequestsPerformance,
  getRecentRequestsPerformanceTrend,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { EndpointTrendCharts } from './EndpointTrendCharts';

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
    // Tolerance rather than `tps % 1000 === 0`: these are floats, so an exact
    // thousand can arrive as 1000.0000000000001 and print as "1.0k".
    return `${k.toFixed(Math.abs(k - Math.round(k)) < 0.001 ? 0 : 1)}k`;
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

/**
 * Identity of a served route, for React keys and the per-route trend state.
 *
 * JSON rather than `model + '|' + endpoint`: both halves are free-form — an
 * admin-supplied route_id becomes the endpoint id verbatim — so a separator
 * that can appear inside either half makes ("a|b", "c") and ("a", "b|c") the
 * same key, and those two routes would then share expansion state and each
 * other's charts.
 */
export function routeKeyOf(route: { model_id: string; endpoint_id: string }): string {
  return JSON.stringify([route.model_id, route.endpoint_id]);
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
  // Expanding a route reveals its trend, fetched for that route alone and kept
  // per route so reopening one is free. Keyed by model *and* endpoint: an
  // endpoint id is not unique on its own — an admin-supplied route_id is
  // per-model, and rows predating served_endpoint_id fall back to a shared
  // provider label — so keying on the endpoint alone would expand two models
  // together and show one of them the other's numbers.
  const [expanded, setExpanded] = useState<string | null>(null);
  const [trendByRoute, setTrendByRoute] = useState<
    Map<string, { series: AdminRequestPerfTrendSeries; bucketMinutes: number; days: number }>
  >(() => new Map());
  // Per route, not one shared marker: with two routes open, the first to answer
  // would otherwise clear the second's "loading" and show its error under the
  // second's chart — and, with the marker cleared, reopening the still-pending
  // route would fire a second copy of an expensive query.
  const [trendLoading, setTrendLoading] = useState<ReadonlySet<string>>(() => new Set());
  const [trendErrors, setTrendErrors] = useState<ReadonlyMap<string, string>>(() => new Map());

  // Monotonic id so a slow response issued under older filters can't overwrite
  // a newer one (the filters change as the admin types).
  const seqRef = useRef(0);
  // Last refreshKey acted on, so a reload driven by Refresh can be told apart
  // from one driven by a filter change: only the former bypasses the backend's
  // short-lived per-filter cache.
  const refreshKeyRef = useRef(refreshKey);
  // Monotonic id for trend fetches, bumped on every filter change, so a response
  // issued under filters that no longer apply is dropped instead of being shown.
  const trendSeqRef = useRef(0);
  // Set when a reload came from Refresh. The backend caches trends for ~20s per
  // filter tuple, so without this the chart opened right after a Refresh can be
  // older than the summary sitting above it.
  const trendNeedsRefreshRef = useRef(false);

  const load = useCallback(
    async (refresh: boolean) => {
      const seq = ++seqRef.current;
      setLoading(true);
      try {
        // Any trend in flight described the previous filters; bump the guard so
        // its response is discarded rather than restored into the new view.
        trendSeqRef.current += 1;
        trendNeedsRefreshRef.current = refresh;
        setExpanded(null);
        setTrendByRoute(new Map());
        setTrendLoading(new Set());
        setTrendErrors(new Map());
        const data = await getRecentRequestsPerformance({
          days,
          userId: userFilter || undefined,
          modelId: modelFilter || undefined,
          requestType: requestType === 'all' ? undefined : requestType,
          refresh,
        });
        if (seq !== seqRef.current) return;
        setGroups(data.groups);
        setTruncated(data.truncated);
        setError(null);
      } catch (e) {
        // Reported inline rather than as a toast: this panel refetches on every
        // filter change, and a repeated toast per keystroke would bury the
        // list's own errors.
        if (seq === seqRef.current) {
          setError(getErrorMessage(e));
          setGroups([]);
          setTruncated(false);
        }
      } finally {
        if (seq === seqRef.current) setLoading(false);
      }
    },
    [days, userFilter, modelFilter, requestType],
  );

  useEffect(() => {
    const isRefresh = refreshKey !== refreshKeyRef.current;
    refreshKeyRef.current = refreshKey;
    load(isRefresh);
  }, [load, refreshKey]);

  const toggleExpanded = useCallback(
    (group: AdminRequestPerfGroup) => {
      const routeKey = routeKeyOf(group);
      const next = expanded === routeKey ? null : routeKey;
      setExpanded(next);
      if (next === null || trendByRoute.has(routeKey) || trendLoading.has(routeKey)) return;

      const seq = trendSeqRef.current;
      // Every route's first fetch after a Refresh bypasses the backend cache;
      // trendByRoute was just cleared, so each route fetches exactly once and
      // the flag stands until the next filter-driven load clears it.
      const refresh = trendNeedsRefreshRef.current;
      setTrendLoading((prev) => new Set(prev).add(routeKey));
      setTrendErrors((prev) => {
        if (!prev.has(routeKey)) return prev;
        const next = new Map(prev);
        next.delete(routeKey);
        return next;
      });
      getRecentRequestsPerformanceTrend({
        days,
        userId: userFilter || undefined,
        modelId: modelFilter || undefined,
        requestType: requestType === 'all' ? undefined : requestType,
        servedModel: group.model_id,
        servedEndpoint: group.endpoint_id,
        refresh,
      })
        .then((data) => {
          if (seq !== trendSeqRef.current) return;
          const series = data.series.find(
            (s) => s.model_id === group.model_id && s.endpoint_id === group.endpoint_id,
          );
          if (series) {
            setTrendByRoute((prev) =>
              new Map(prev).set(routeKey, {
                series,
                bucketMinutes: data.bucket_minutes,
                days: data.days,
              }),
            );
          }
        })
        .catch((e) => {
          if (seq !== trendSeqRef.current) return;
          setTrendErrors((prev) => new Map(prev).set(routeKey, getErrorMessage(e)));
        })
        .finally(() => {
          if (seq !== trendSeqRef.current) return;
          setTrendLoading((prev) => {
            const next = new Set(prev);
            next.delete(routeKey);
            return next;
          });
        });
    },
    [expanded, trendByRoute, trendLoading, days, userFilter, modelFilter, requestType],
  );

  return (
    <div className="mt-4 rounded-2xl border border-gray-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-100 px-4 py-3">
        <div>
          <h2 className="text-[14px] font-semibold text-gray-900">
            Per-endpoint TTFT &amp; decode throughput
          </h2>
          <p className="text-[11px] text-gray-400">
            Successful streaming requests over the last {days}d, split by the model and endpoint
            that served them. Select a route to see it over time
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
          {/* The cap keeps a long route list from dominating the tab, but an
              expanded chart is ~20rem on its own — leaving the cap in place
              scrolls every other route out of view, which is the comparison the
              panel exists for. */}
          <div className={`overflow-auto ${expanded ? 'max-h-[56rem]' : 'max-h-[22rem]'}`}>
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
                {groups.map((group) => {
                  const routeKey = routeKeyOf(group);
                  const isExpanded = expanded === routeKey;
                  const routeTrend = trendByRoute.get(routeKey);
                  const routeError = trendErrors.get(routeKey);
                  return (
                    <Fragment key={routeKey}>
                      <tr
                        className="cursor-pointer border-b border-gray-100 last:border-b-0 hover:bg-gray-50/60"
                        onClick={() => toggleExpanded(group)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            toggleExpanded(group);
                          }
                        }}
                        tabIndex={0}
                        role="button"
                        aria-expanded={isExpanded}
                        aria-label={`Show the last ${days}d trend for ${group.endpoint_id}`}
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
                      {isExpanded && (
                        <tr className="border-b border-gray-100 bg-gray-50/40">
                          <td colSpan={11} className="px-4 py-3">
                            {trendLoading.has(routeKey) && !routeTrend ? (
                              <p className="py-6 text-center text-[12px] text-gray-400">
                                Loading trend…
                              </p>
                            ) : routeError && !routeTrend ? (
                              <p className="py-6 text-center text-[12px] text-red-600">
                                Failed to load the trend: {routeError}
                              </p>
                            ) : routeTrend ? (
                              <EndpointTrendCharts
                                series={routeTrend.series}
                                bucketMinutes={routeTrend.bucketMinutes}
                                days={routeTrend.days}
                              />
                            ) : (
                              <p className="py-6 text-center text-[12px] text-gray-400">
                                No measurable traffic for this route in the window.
                              </p>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
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
