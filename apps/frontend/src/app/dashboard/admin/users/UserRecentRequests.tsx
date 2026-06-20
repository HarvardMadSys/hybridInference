'use client';

import { Fragment, useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import {
  AdminRecentRequestItem,
  getRecentRequestContent,
  listRecentRequests,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { formatRouteWiseDecision } from '@/lib/utils/routewise';
import { InlineErrorText } from '@/components/ui/InlineErrorText';
import { FoldedText } from '@/components/features/admin/requestContent';

const PAGE_SIZE = 25;

function relTime(s: string | null): string {
  if (!s) return 'Never';
  const ms = Date.now() - new Date(s).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatLatency(ms?: number | null): string {
  if (ms == null) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

function formatCost(cost?: number | null): string {
  if (cost == null) return '—';
  return cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`;
}

interface RequestContentState {
  prompt: string | null;
  response: string | null;
  reasoning_content: string | null;
  loading: boolean;
  error?: string;
}

/**
 * Recent LLM requests for a single user, shown inside the admin user detail
 * panel. Fetches `/admin/recent-requests?user_id=<id>` (exact id is a substring
 * of itself, so the backend's substring match resolves to this user) and lets
 * the admin expand a row to inspect the prompt/response payload on demand.
 */
export function UserRecentRequests({ userId }: { userId: string }) {
  const [entries, setEntries] = useState<AdminRecentRequestItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [contentCache, setContentCache] = useState<Map<string, RequestContentState>>(
    () => new Map(),
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await listRecentRequests(PAGE_SIZE, offset, userId);
      setEntries(d.requests);
      setTotal(d.total);
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [userId, offset]);

  useEffect(() => {
    load();
  }, [load]);

  const toggleRow = useCallback(
    (requestId: string) => {
      const next = expandedId === requestId ? null : requestId;
      setExpandedId(next);
      if (next === null || contentCache.has(next)) return;
      setContentCache((prev) => {
        if (prev.has(next)) return prev;
        const updated = new Map(prev);
        updated.set(next, {
          prompt: null,
          response: null,
          reasoning_content: null,
          loading: true,
        });
        return updated;
      });
      getRecentRequestContent(next)
        .then((content) => {
          setContentCache((prev) => {
            const updated = new Map(prev);
            updated.set(next, {
              prompt: content.prompt,
              response: content.response,
              reasoning_content: content.reasoning_content,
              loading: false,
            });
            return updated;
          });
        })
        .catch((e) => {
          setContentCache((prev) => {
            const updated = new Map(prev);
            updated.set(next, {
              prompt: null,
              response: null,
              reasoning_content: null,
              loading: false,
              error: getErrorMessage(e),
            });
            return updated;
          });
        });
    },
    [expandedId, contentCache],
  );

  return (
    <div className="space-y-2 border-t border-gray-200 pt-4">
      <div className="flex items-center justify-between">
        <div className="text-[11px] font-medium uppercase tracking-wide text-gray-500">
          Recent requests
        </div>
        <div className="flex items-center gap-2">
          {loading && (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
          <span className="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium tabular-nums text-gray-600">
            {total.toLocaleString()} total
          </span>
          <button
            type="button"
            onClick={load}
            disabled={loading}
            className="rounded-md border border-gray-200 bg-white px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            Refresh
          </button>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-gray-200 bg-white">
        {loading && entries.length === 0 ? (
          <div className="flex items-center justify-center py-8">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          </div>
        ) : entries.length === 0 ? (
          <div className="py-8 text-center text-[12px] text-gray-400">No requests yet.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full">
              <thead className="bg-gray-50/80">
                <tr className="border-b border-gray-200">
                  <th className="py-2 pl-3 pr-2 text-left text-[10px] font-medium uppercase tracking-wider text-gray-500">
                    Model
                  </th>
                  <th className="px-2 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-gray-500">
                    Status
                  </th>
                  <th className="px-2 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-gray-500">
                    Latency
                  </th>
                  <th className="px-2 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-gray-500">
                    Tokens
                  </th>
                  <th className="px-2 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-gray-500">
                    Cost
                  </th>
                  <th className="px-2 py-2 pr-3 text-right text-[10px] font-medium uppercase tracking-wider text-gray-500">
                    Time
                  </th>
                </tr>
              </thead>
              <tbody>
                {entries.map((req) => {
                  const isSuccess =
                    req.status_code != null && req.status_code >= 200 && req.status_code < 400;
                  const isExpanded = expandedId === req.request_id;
                  const routewiseDecision = formatRouteWiseDecision(req);
                  return (
                    <Fragment key={req.request_id}>
                      <tr
                        className="cursor-pointer border-b border-gray-100 transition-colors hover:bg-gray-50/60"
                        onClick={() => toggleRow(req.request_id)}
                      >
                        <td className="py-2 pl-3 pr-2 text-[12px]">
                          <div className="flex items-center gap-1.5 font-medium text-gray-900">
                            <span className="whitespace-nowrap">{req.model_id}</span>
                            {req.request_type === 'embedding' && (
                              <span className="inline-flex items-center rounded-md bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 ring-1 ring-inset ring-violet-600/20">
                                embedding
                              </span>
                            )}
                          </div>
                          <div className="text-[10px] text-gray-400">{req.provider}</div>
                          {req.error && <InlineErrorText message={req.error} size="xs" />}
                        </td>
                        <td className="whitespace-nowrap px-2 py-2 text-[12px]">
                          {req.status_code != null ? (
                            <span
                              className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ${
                                isSuccess
                                  ? 'bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20'
                                  : 'bg-red-50 text-red-700 ring-1 ring-inset ring-red-600/20'
                              }`}
                            >
                              {req.status_code}
                            </span>
                          ) : (
                            <span className="text-gray-300">—</span>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-2 py-2 text-[12px] tabular-nums text-gray-600">
                          {formatLatency(req.latency_ms)}
                        </td>
                        <td className="whitespace-nowrap px-2 py-2 text-[12px] tabular-nums text-gray-600">
                          {req.prompt_tokens != null || req.completion_tokens != null ? (
                            <>
                              <span className="text-gray-400">↑</span>
                              {(req.prompt_tokens ?? 0).toLocaleString()}
                              <span className="mx-0.5 text-gray-300">/</span>
                              <span className="text-gray-400">↓</span>
                              {(req.completion_tokens ?? 0).toLocaleString()}
                            </>
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className="whitespace-nowrap px-2 py-2 text-[12px] tabular-nums text-gray-600">
                          {formatCost(req.cost_usd)}
                        </td>
                        <td className="whitespace-nowrap px-2 py-2 pr-3 text-right text-[12px] text-gray-400">
                          <span title={new Date(req.timestamp).toLocaleString()}>
                            {relTime(req.timestamp)}
                          </span>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="border-b border-gray-100 bg-gray-50/40">
                          <td colSpan={6} className="px-3 py-3">
                            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-[11px] sm:grid-cols-4">
                              <div>
                                <span className="text-gray-500">Request ID:</span>{' '}
                                <span className="font-mono text-gray-700">
                                  {req.request_id.length > 24
                                    ? `${req.request_id.slice(0, 24)}…`
                                    : req.request_id}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-500">TTFT:</span>{' '}
                                <span className="text-gray-700">
                                  {req.ttft_ms != null ? `${req.ttft_ms}ms` : '—'}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-500">Stream:</span>{' '}
                                <span className="text-gray-700">
                                  {req.stream != null ? (req.stream ? 'Yes' : 'No') : '—'}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-500">Client:</span>{' '}
                                <span className="font-mono text-gray-700 break-all">
                                  {req.user_agent || '—'}
                                </span>
                              </div>
                              {routewiseDecision && (
                                <div className="col-span-full">
                                  <span className="text-gray-500">RouteWise:</span>{' '}
                                  <span className="font-mono text-gray-700 break-all">
                                    {routewiseDecision}
                                  </span>
                                </div>
                              )}
                              {(() => {
                                const content = contentCache.get(req.request_id);
                                if (!content || content.loading) {
                                  return (
                                    <div className="col-span-full text-gray-400">
                                      Loading prompt and response…
                                    </div>
                                  );
                                }
                                if (content.error) {
                                  return (
                                    <div className="col-span-full text-red-600">
                                      Failed to load content: {content.error}
                                    </div>
                                  );
                                }
                                return (
                                  <>
                                    {content.reasoning_content && (
                                      <FoldedText
                                        label="Reasoning"
                                        value={content.reasoning_content}
                                      />
                                    )}
                                    <FoldedText label="Prompt" value={content.prompt} />
                                    <FoldedText label="Response" value={content.response} />
                                  </>
                                );
                              })()}
                              {req.error && (
                                <div className="col-span-full mt-1">
                                  <span className="text-red-600">Error: {req.error}</span>
                                </div>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {total > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t border-gray-100 bg-gray-50/40 px-3 py-2">
            <span className="text-[11px] tabular-nums text-gray-400">
              {offset + 1}&ndash;{Math.min(offset + PAGE_SIZE, total)} of {total.toLocaleString()}
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0 || loading}
                className="rounded-md px-2 py-1 text-[11px] font-medium text-gray-500 transition hover:bg-gray-100 disabled:opacity-30"
              >
                Prev
              </button>
              <button
                type="button"
                onClick={() => setOffset(offset + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= total || loading}
                className="rounded-md px-2 py-1 text-[11px] font-medium text-gray-500 transition hover:bg-gray-100 disabled:opacity-30"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
