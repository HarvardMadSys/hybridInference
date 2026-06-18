'use client';

import { Fragment, useCallback, useEffect, useId, useRef, useState } from 'react';
import toast from 'react-hot-toast';
import {
  AdminRecentRequestItem,
  AdminRequestMetricsWindow,
  clearErrorRequests,
  exportRequests,
  getRecentRequestContent,
  getRequestMetrics,
  listRecentRequests,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { formatRouteWiseDecision } from '@/lib/utils/routewise';
import { InlineErrorText } from '@/components/ui/InlineErrorText';
import { FoldedText } from './requestContent';

const REQ_PAGE_SIZE = 50;

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

function parseClientTool(ua: string | null | undefined): string | null {
  if (!ua) return null;
  const s = ua.trim();
  if (!s) return null;
  const patterns: Array<[RegExp, string]> = [
    [/claude-cli\/|claude-code\//i, 'claude-code'],
    [/kilo[-_ ]?code\//i, 'kilo-code'],
    [/roo[-_ ]?code\//i, 'roo-code'],
    [/cline\//i, 'cline'],
    [/cursor[-_ ]?(ide|agent|cli)?\//i, 'cursor'],
    [/aider\//i, 'aider'],
    [/continue\//i, 'continue'],
    [/codex[-_ ]?cli\//i, 'codex'],
    [/openai[-_ ]?python\/|openai\/python/i, 'openai-python'],
    [/openai[-_ ]?node\/|openai\/javascript/i, 'openai-node'],
    [/anthropic[-_ ]?python\//i, 'anthropic-python'],
    [/anthropic[-_ ]?(sdk|ts|js)\//i, 'anthropic-sdk'],
    [/postmanruntime\//i, 'postman'],
    [/insomnia\//i, 'insomnia'],
    [/httpie\//i, 'httpie'],
    [/curl\//i, 'curl'],
    [/wget\//i, 'wget'],
    [/python-requests\//i, 'python-requests'],
    [/aiohttp\//i, 'aiohttp'],
    [/httpx\//i, 'httpx'],
    [/node-fetch\//i, 'node-fetch'],
    [/axios\//i, 'axios'],
    [/go-http-client\//i, 'go-http'],
    [/okhttp\//i, 'okhttp'],
  ];
  for (const [re, name] of patterns) {
    if (re.test(s)) return name;
  }
  if (/mozilla\/|chrome\/|safari\/|firefox\/|edg\//i.test(s)) return 'browser';
  const m = s.match(/^([A-Za-z][\w.-]{1,32})\//);
  if (m) return m[1].toLowerCase();
  return null;
}

function applyOffsetJump(
  rawPage: string,
  total: number,
  pageSize: number,
  setOffset: (offset: number) => void,
  clearInput: () => void,
): void {
  const totalPages = Math.ceil(total / pageSize);
  const n = Number.parseInt(rawPage.trim(), 10);
  if (!Number.isFinite(n)) return;
  const p = Math.min(Math.max(1, n), totalPages);
  setOffset((p - 1) * pageSize);
  clearInput();
}

function formatLatency(ms?: number | null): string {
  if (ms == null) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

function formatTokens(n: number): string {
  return Math.round(n).toLocaleString();
}

function SearchInput({
  value,
  onChange,
  onSubmit,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit?: () => void;
  placeholder: string;
}) {
  // Controlled and synchronous: the parent filter state always reflects what the
  // input shows, so actions like Refresh/Export never read a stale value. The
  // per-keystroke fetch is debounced in the parent instead (see loadRequests).
  return (
    <div className="relative flex-1 min-w-[160px]">
      <svg
        className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="m21 21-4.35-4.35M17 11a6 6 0 1 1-12 0 6 6 0 0 1 12 0Z"
        />
      </svg>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') onSubmit?.();
        }}
        placeholder={placeholder}
        aria-label={placeholder}
        className="w-full rounded-lg border border-gray-200 bg-white py-2 pl-9 pr-3 text-[13px] placeholder:text-gray-400 transition-shadow focus:border-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-900/5"
      />
    </div>
  );
}

function RequestMetricsCard({ metric }: { metric: AdminRequestMetricsWindow }) {
  const maxRequests = Math.max(...metric.buckets.map((bucket) => bucket.request_count), 1);

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[12px] font-medium text-gray-500">{metric.label}</div>
          <div className="mt-1 text-[24px] font-bold tabular-nums text-gray-900">
            {metric.total_requests.toLocaleString()}
          </div>
          <div className="text-[11px] text-gray-400">requests</div>
        </div>
        <div className="text-right text-[11px] text-gray-400">
          <div>
            <span className="text-emerald-600">{metric.success_requests.toLocaleString()}</span> ok
          </div>
          <div>
            <span className="text-red-500">{metric.error_requests.toLocaleString()}</span> err
          </div>
          <div>{formatLatency(metric.avg_latency_ms)} avg</div>
        </div>
      </div>
      <div className="mt-4 flex h-16 items-end gap-px overflow-hidden rounded-md bg-gray-50 px-1 py-1">
        {metric.buckets.map((bucket) => {
          const height =
            bucket.request_count === 0 ? 2 : (bucket.request_count / maxRequests) * 100;
          const isErrorHeavy = bucket.error_count > 0 && bucket.error_count >= bucket.success_count;
          return (
            <div
              key={bucket.start_time}
              className={`min-w-0 flex-1 rounded-t-sm ${
                isErrorHeavy ? 'bg-red-400' : 'bg-gray-900'
              }`}
              style={{ height: `${height}%` }}
              title={`${new Date(bucket.start_time).toLocaleString()}: ${
                bucket.request_count
              } requests, ${bucket.error_count} errors`}
            />
          );
        })}
      </div>
    </div>
  );
}

export function RequestsTab() {
  // Requests state
  const [reqEntries, setReqEntries] = useState<AdminRecentRequestItem[]>([]);
  const [reqTotal, setReqTotal] = useState(0);
  const [reqLoading, setReqLoading] = useState(false);
  const [reqOffset, setReqOffset] = useState(0);
  const [reqUserFilter, setReqUserFilter] = useState('');
  const [reqModelFilter, setReqModelFilter] = useState('');
  // Debounced copies of the text filters drive the fetch, so typing doesn't fire
  // a request per keystroke. The raw values above stay synchronous for the
  // inputs and for actions (Refresh/Export) that must read the current filters.
  const [debouncedUserFilter, setDebouncedUserFilter] = useState('');
  const [debouncedModelFilter, setDebouncedModelFilter] = useState('');
  const [reqErrorsOnly, setReqErrorsOnly] = useState(false);
  const [reqExpandedId, setReqExpandedId] = useState<string | null>(null);
  const [reqContentCache, setReqContentCache] = useState<
    Map<
      string,
      {
        prompt: string | null;
        response: string | null;
        reasoning_content: string | null;
        loading: boolean;
        error?: string;
      }
    >
  >(() => new Map());
  const [reqJumpPage, setReqJumpPage] = useState('');
  const [reqMetrics, setReqMetrics] = useState<AdminRequestMetricsWindow[]>([]);
  const [reqMetricsLoading, setReqMetricsLoading] = useState(false);
  const reqJumpInputId = useId();
  const [showExportPanel, setShowExportPanel] = useState(false);
  const [exportStartDate, setExportStartDate] = useState('');
  const [exportEndDate, setExportEndDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [exportIncludeContent, setExportIncludeContent] = useState(false);
  const [exportLoading, setExportLoading] = useState(false);
  const [clearingErrors, setClearingErrors] = useState(false);

  // Monotonic id for list fetches so an out-of-order response (e.g. a slow
  // request issued under an older filter) can't overwrite a newer one.
  const reqSeqRef = useRef(0);

  // Callers pass the filters to fetch with: the debounce effect passes the
  // debounced values, while manual actions (Refresh, Clear errors) pass the
  // raw visible filters so they always reflect what the admin sees.
  const loadRequests = useCallback(
    async (userFilter: string, modelFilter: string) => {
      const seq = ++reqSeqRef.current;
      setReqLoading(true);
      try {
        const d = await listRecentRequests(
          REQ_PAGE_SIZE,
          reqOffset,
          userFilter || undefined,
          modelFilter || undefined,
          reqErrorsOnly,
        );
        if (seq !== reqSeqRef.current) return;
        setReqEntries(d.requests);
        setReqTotal(d.total);
      } catch (e) {
        if (seq === reqSeqRef.current) toast.error(getErrorMessage(e));
      } finally {
        if (seq === reqSeqRef.current) setReqLoading(false);
      }
    },
    [reqOffset, reqErrorsOnly],
  );

  // Debounce text-filter changes into the values that drive the fetch. Offset
  // and "errors only" changes are applied immediately (they don't go through
  // here), so pagination stays snappy.
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedUserFilter(reqUserFilter);
      setDebouncedModelFilter(reqModelFilter);
    }, 300);
    return () => clearTimeout(timer);
  }, [reqUserFilter, reqModelFilter]);

  // Flush the debounce so pressing Enter searches immediately.
  const flushFilterSearch = useCallback(() => {
    setDebouncedUserFilter(reqUserFilter);
    setDebouncedModelFilter(reqModelFilter);
  }, [reqUserFilter, reqModelFilter]);

  const loadRequestMetrics = useCallback(async () => {
    setReqMetricsLoading(true);
    try {
      const d = await getRequestMetrics();
      setReqMetrics(d.windows);
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setReqMetricsLoading(false);
    }
  }, []);

  const handleClearErrors = useCallback(async () => {
    if (
      !window.confirm(
        'Permanently delete all error requests from the past hour? This cannot be undone.',
      )
    ) {
      return;
    }
    setClearingErrors(true);
    try {
      const result = await clearErrorRequests(1);
      toast.success(result.message);
      loadRequests(reqUserFilter, reqModelFilter);
      loadRequestMetrics();
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setClearingErrors(false);
    }
  }, [loadRequests, loadRequestMetrics, reqUserFilter, reqModelFilter]);

  useEffect(() => {
    loadRequests(debouncedUserFilter, debouncedModelFilter);
  }, [loadRequests, debouncedUserFilter, debouncedModelFilter]);

  useEffect(() => {
    loadRequestMetrics();
  }, [loadRequestMetrics]);

  const handleToggleRequestRow = useCallback(
    (requestId: string) => {
      // Compute the next expanded id and decide whether to fetch *outside*
      // the state updater so React strict-mode's double-invoke of updaters
      // can't kick off duplicate fetches or race the cache.
      const next = reqExpandedId === requestId ? null : requestId;
      setReqExpandedId(next);
      if (next === null) return;
      if (reqContentCache.has(next)) return;
      setReqContentCache((prev) => {
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
          setReqContentCache((prev) => {
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
          setReqContentCache((prev) => {
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
    [reqExpandedId, reqContentCache],
  );

  return (
    <div className="mt-6">
      {/* Request metrics */}
      <div className="mb-6">
        <div className="mb-3 flex items-center justify-between">
          <div>
            <h2 className="text-[15px] font-semibold text-gray-900">Request volume</h2>
            <p className="text-[12px] text-gray-400">
              Traffic trends across short and long lookback windows.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {(reqMetricsLoading || reqLoading) && (
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
            )}
            <button
              type="button"
              onClick={() => {
                loadRequestMetrics();
                loadRequests(reqUserFilter, reqModelFilter);
              }}
              disabled={reqMetricsLoading || reqLoading}
              className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50 disabled:opacity-50"
            >
              Refresh
            </button>
          </div>
        </div>
        {reqMetrics.length > 0 ? (
          <div className="grid gap-3 sm:grid-cols-2">
            {reqMetrics.map((metric) => (
              <RequestMetricsCard key={metric.key} metric={metric} />
            ))}
          </div>
        ) : !reqMetricsLoading ? (
          <div className="rounded-xl border border-dashed border-gray-200 py-8 text-center">
            <p className="text-[13px] text-gray-400">No request metrics available.</p>
          </div>
        ) : null}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-gray-200 bg-white p-3 shadow-sm">
        <SearchInput
          value={reqUserFilter}
          onChange={(value) => {
            setReqUserFilter(value);
            setReqOffset(0);
          }}
          onSubmit={flushFilterSearch}
          placeholder="Filter by user ID, name, or email…"
        />
        <SearchInput
          value={reqModelFilter}
          onChange={(value) => {
            setReqModelFilter(value);
            setReqOffset(0);
          }}
          onSubmit={flushFilterSearch}
          placeholder="Filter by model…"
        />
        <label className="flex cursor-pointer select-none items-center gap-1.5 text-[13px] text-gray-600">
          <input
            type="checkbox"
            checked={reqErrorsOnly}
            onChange={(e) => {
              setReqErrorsOnly(e.target.checked);
              setReqOffset(0);
            }}
            className="rounded border-gray-300 text-gray-900 focus:ring-gray-400"
          />
          Errors only
        </label>
        <span className="inline-flex items-center rounded-full bg-gray-100 px-2.5 py-1 text-[12px] font-medium tabular-nums text-gray-600">
          {reqTotal.toLocaleString()} entries
        </span>
        <button
          type="button"
          onClick={handleClearErrors}
          disabled={clearingErrors}
          className="ml-auto rounded-lg border border-red-200 bg-white px-3 py-2 text-[13px] font-medium text-red-600 transition-colors hover:bg-red-50 disabled:opacity-50"
        >
          {clearingErrors ? 'Clearing…' : 'Clear last hour errors'}
        </button>
        <button
          type="button"
          onClick={() => setShowExportPanel((v) => !v)}
          className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] font-medium text-gray-600 transition-colors hover:bg-gray-50"
        >
          Export JSONL
        </button>
      </div>

      {showExportPanel && (
        <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-4">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-gray-500">Start date</span>
              <input
                type="date"
                value={exportStartDate}
                onChange={(e) => setExportStartDate(e.target.value)}
                className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-gray-500">End date</span>
              <input
                type="date"
                value={exportEndDate}
                onChange={(e) => setExportEndDate(e.target.value)}
                className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
              />
            </label>
            <label className="flex items-center gap-1.5 pb-2 text-[13px] text-gray-600 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={exportIncludeContent}
                onChange={(e) => setExportIncludeContent(e.target.checked)}
                className="rounded border-gray-300"
              />
              Include prompt &amp; response
            </label>
            <div className="ml-auto flex items-center gap-2 pb-2">
              <button
                type="button"
                onClick={() => {
                  setShowExportPanel(false);
                  setExportStartDate('');
                  setExportEndDate(new Date().toISOString().slice(0, 10));
                  setExportIncludeContent(false);
                }}
                className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={
                  !exportStartDate ||
                  exportLoading ||
                  (!!exportEndDate && exportEndDate < exportStartDate)
                }
                onClick={async () => {
                  if (!exportStartDate) return;
                  setExportLoading(true);
                  try {
                    await exportRequests({
                      startTime: new Date(`${exportStartDate}T00:00:00Z`).toISOString(),
                      endTime: exportEndDate
                        ? new Date(`${exportEndDate}T23:59:59Z`).toISOString()
                        : undefined,
                      userId: reqUserFilter || undefined,
                      modelId: reqModelFilter || undefined,
                      errorsOnly: reqErrorsOnly || undefined,
                      includeContent: exportIncludeContent || undefined,
                    });
                    setShowExportPanel(false);
                  } catch (err) {
                    toast.error(
                      `Export failed: ${err instanceof Error ? err.message : 'Unknown error'}`,
                    );
                  } finally {
                    setExportLoading(false);
                  }
                }}
                className="rounded-lg bg-gray-900 px-3 py-2 text-[13px] text-white hover:bg-gray-700 disabled:opacity-50"
              >
                {exportLoading ? 'Exporting…' : 'Export'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Table */}
      <div className="mt-4 overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
        {reqLoading ? (
          <div className="flex flex-col items-center justify-center gap-3 py-24">
            <span className="h-6 w-6 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
            <p className="text-[12px] text-gray-400">Loading requests…</p>
          </div>
        ) : reqEntries.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-24 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-gray-50 ring-1 ring-inset ring-gray-100">
              <svg
                className="h-6 w-6 text-gray-300"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.5}
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M2.25 13.5h3.86a2.25 2.25 0 0 1 2.012 1.244l.256.512a2.25 2.25 0 0 0 2.013 1.244h3.218a2.25 2.25 0 0 0 2.013-1.244l.256-.512a2.25 2.25 0 0 1 2.013-1.244h3.859m-19.5.338V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 0 0-2.15-1.588H6.911a2.25 2.25 0 0 0-2.15 1.588L2.35 13.177a2.25 2.25 0 0 0-.1.661Z"
                />
              </svg>
            </div>
            <p className="text-[13px] font-medium text-gray-600">No requests found</p>
            <p className="text-[12px] text-gray-400">
              Try adjusting your filters or the lookback window.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full">
              <thead className="bg-gray-50/80">
                <tr className="border-b border-gray-200">
                  <th className="py-2 pl-4 pr-3 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Model
                  </th>
                  <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    User
                  </th>
                  <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Client
                  </th>
                  <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Status
                  </th>
                  <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Latency
                  </th>
                  <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Decode
                  </th>
                  <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Tokens
                  </th>
                  <th
                    className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500"
                    title="Conversation turns: total messages, user turns, and tool calls"
                  >
                    Turns
                  </th>
                  <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                    Cost
                  </th>
                  <th className="px-3 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-gray-500 pr-4">
                    Time
                  </th>
                </tr>
              </thead>
              <tbody>
                {reqEntries.map((req) => {
                  const isSuccess =
                    req.status_code != null && req.status_code >= 200 && req.status_code < 400;
                  const isExpanded = reqExpandedId === req.request_id;
                  const cachedTokens = req.cache_read_tokens ?? null;
                  const routewiseDecision = formatRouteWiseDecision(req);
                  return (
                    <Fragment key={req.request_id}>
                      <tr
                        className="border-b border-gray-100 hover:bg-gray-50/60 cursor-pointer transition-colors"
                        onClick={() => handleToggleRequestRow(req.request_id)}
                      >
                        <td className="py-2.5 pl-4 pr-3 text-[13px]">
                          <div className="flex items-center gap-1.5 font-medium text-gray-900">
                            <span className="whitespace-nowrap">{req.model_id}</span>
                            {req.request_type === 'embedding' && (
                              <span className="inline-flex items-center rounded-md bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 ring-1 ring-inset ring-violet-600/20">
                                embedding
                              </span>
                            )}
                            {req.reasoning_tokens != null && req.reasoning_tokens > 0 && (
                              <span
                                className="inline-flex items-center rounded-md bg-purple-50 px-1.5 py-0.5 text-[10px] font-medium text-purple-700 ring-1 ring-inset ring-purple-600/20"
                                title={`${req.reasoning_tokens.toLocaleString()} reasoning tokens`}
                              >
                                R {formatTokens(req.reasoning_tokens)}
                              </span>
                            )}
                          </div>
                          <div className="text-[11px] text-gray-400">{req.provider}</div>
                          {req.error && <InlineErrorText message={req.error} size="xs" />}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[12px] font-mono text-gray-500">
                          {req.user_id ? (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setReqUserFilter(req.user_id!);
                                setReqOffset(0);
                              }}
                              className="block max-w-[220px] text-left transition hover:text-gray-900 hover:underline"
                              title={`${req.user_name || req.user_id}${
                                req.user_email ? ` <${req.user_email}>` : ''
                              }`}
                            >
                              <span className="block truncate font-sans text-[13px] font-medium text-gray-800">
                                {req.user_name || req.user_id}
                              </span>
                              {req.user_email ? (
                                <span className="block truncate text-[11px] text-gray-400">
                                  {req.user_email}
                                </span>
                              ) : (
                                <span className="block truncate text-[11px] text-gray-400">
                                  {req.user_id.slice(0, 12)}…
                                </span>
                              )}
                            </button>
                          ) : (
                            <span className="text-gray-300">—</span>
                          )}
                        </td>
                        <td className="px-3 py-2.5 text-[12px] text-gray-600">
                          {(() => {
                            const tool = parseClientTool(req.user_agent);
                            if (tool) {
                              return (
                                <span
                                  className="inline-flex max-w-[220px] truncate rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[11px] text-gray-700"
                                  title={req.user_agent ?? undefined}
                                >
                                  {tool}
                                </span>
                              );
                            }
                            if (req.user_agent) {
                              return (
                                <span
                                  className="block max-w-[220px] truncate text-gray-500"
                                  title={req.user_agent}
                                >
                                  {req.user_agent}
                                </span>
                              );
                            }
                            return <span className="text-gray-300">—</span>;
                          })()}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[12px]">
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
                        <td className="whitespace-nowrap px-3 py-2.5 text-[12px] tabular-nums text-gray-600">
                          {req.latency_ms != null
                            ? req.latency_ms >= 1000
                              ? `${(req.latency_ms / 1000).toFixed(1)}s`
                              : `${req.latency_ms}ms`
                            : '—'}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[12px] tabular-nums text-gray-600">
                          {req.decode_throughput_tps != null
                            ? `${req.decode_throughput_tps.toFixed(1)} tok/s`
                            : '—'}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[12px] tabular-nums text-gray-600">
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
                        <td className="whitespace-nowrap px-3 py-2.5 text-[12px] tabular-nums text-gray-600">
                          {req.num_turns != null ? (
                            <div className="flex items-center gap-1.5">
                              <span
                                className="font-medium text-gray-700"
                                title="Total messages in the conversation"
                              >
                                {req.num_turns.toLocaleString()}
                              </span>
                              <span
                                className="inline-flex items-center rounded-md bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 ring-1 ring-inset ring-sky-600/15"
                                title={`${(req.num_user_turns ?? 0).toLocaleString()} user turns`}
                              >
                                {(req.num_user_turns ?? 0).toLocaleString()}u
                              </span>
                              {(req.num_tool_calls ?? 0) > 0 && (
                                <span
                                  className="inline-flex items-center rounded-md bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 ring-1 ring-inset ring-amber-600/15"
                                  title={`${(req.num_tool_calls ?? 0).toLocaleString()} tool calls`}
                                >
                                  {(req.num_tool_calls ?? 0).toLocaleString()}t
                                </span>
                              )}
                            </div>
                          ) : (
                            <span className="text-gray-300">—</span>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[12px] tabular-nums text-gray-600">
                          {req.cost_usd != null
                            ? req.cost_usd < 0.01
                              ? `$${req.cost_usd.toFixed(4)}`
                              : `$${req.cost_usd.toFixed(2)}`
                            : '—'}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-right text-[12px] text-gray-400 pr-4">
                          <span title={new Date(req.timestamp).toLocaleString()}>
                            {relTime(req.timestamp)}
                          </span>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="border-b border-gray-100 bg-gray-50/40">
                          <td colSpan={10} className="px-4 py-3">
                            <div className="grid grid-cols-2 gap-x-8 gap-y-1 text-[11px] sm:grid-cols-4">
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
                                <span className="text-gray-500">Cached:</span>{' '}
                                <span className="text-gray-700">
                                  {cachedTokens != null ? cachedTokens.toLocaleString() : '—'}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-500">Stream:</span>{' '}
                                <span className="text-gray-700">
                                  {req.stream != null ? (req.stream ? 'Yes' : 'No') : '—'}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-500">Turns:</span>{' '}
                                <span className="text-gray-700">
                                  {req.num_turns != null ? req.num_turns.toLocaleString() : '—'}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-500">User turns:</span>{' '}
                                <span className="text-gray-700">
                                  {req.num_user_turns != null
                                    ? req.num_user_turns.toLocaleString()
                                    : '—'}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-500">Tool calls:</span>{' '}
                                <span className="text-gray-700">
                                  {req.num_tool_calls != null
                                    ? req.num_tool_calls.toLocaleString()
                                    : '—'}
                                </span>
                              </div>
                              {routewiseDecision && (
                                <div className="col-span-full">
                                  <span className="text-gray-500">RouteWise:</span>{' '}
                                  <span className="text-gray-700 font-mono break-all">
                                    {routewiseDecision}
                                  </span>
                                </div>
                              )}
                              <div>
                                <span className="text-gray-500">User:</span>{' '}
                                <span className="text-gray-700">
                                  {req.user_name || req.user_id || '—'}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-500">Email:</span>{' '}
                                <span className="text-gray-700">{req.user_email || '—'}</span>
                              </div>
                              <div>
                                <span className="text-gray-500">User IP:</span>{' '}
                                <span className="text-gray-700 font-mono">
                                  {req.user_ip ?? '—'}
                                </span>
                              </div>
                              <div className="col-span-full">
                                <span className="text-gray-500">User agent:</span>{' '}
                                <span className="text-gray-700 font-mono break-all">
                                  {req.user_agent || '—'}
                                </span>
                              </div>
                              {(() => {
                                const content = reqContentCache.get(req.request_id);
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

        {/* Pagination */}
        {reqTotal > REQ_PAGE_SIZE && (
          <div className="flex flex-col gap-3 border-t border-gray-100 bg-gray-50/40 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <span className="text-[12px] text-gray-400 tabular-nums text-center sm:text-left">
              {reqOffset + 1}&ndash;{Math.min(reqOffset + REQ_PAGE_SIZE, reqTotal)} of {reqTotal}
              <span className="ml-2 text-gray-300">
                (page {Math.floor(reqOffset / REQ_PAGE_SIZE) + 1} of{' '}
                {Math.ceil(reqTotal / REQ_PAGE_SIZE)})
              </span>
            </span>
            <div className="flex flex-wrap items-center justify-center gap-3 sm:justify-end">
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setReqOffset(Math.max(0, reqOffset - REQ_PAGE_SIZE))}
                  disabled={reqOffset === 0}
                  className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
                >
                  Prev
                </button>
                <button
                  onClick={() => setReqOffset(reqOffset + REQ_PAGE_SIZE)}
                  disabled={reqOffset + REQ_PAGE_SIZE >= reqTotal}
                  className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
                >
                  Next
                </button>
              </div>
              <div className="flex items-center gap-2">
                <label
                  htmlFor={reqJumpInputId}
                  className="text-[12px] text-gray-400 whitespace-nowrap"
                >
                  Jump to page
                </label>
                <input
                  id={reqJumpInputId}
                  type="number"
                  min={1}
                  max={Math.ceil(reqTotal / REQ_PAGE_SIZE)}
                  inputMode="numeric"
                  value={reqJumpPage}
                  onChange={(e) => setReqJumpPage(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      applyOffsetJump(reqJumpPage, reqTotal, REQ_PAGE_SIZE, setReqOffset, () =>
                        setReqJumpPage(''),
                      );
                    }
                  }}
                  className="w-14 rounded-md border border-gray-200 px-2 py-1 text-center text-[12px] text-gray-900 tabular-nums focus:border-gray-400 focus:outline-none"
                  aria-label="Page number to jump to"
                />
                <button
                  type="button"
                  onClick={() =>
                    applyOffsetJump(reqJumpPage, reqTotal, REQ_PAGE_SIZE, setReqOffset, () =>
                      setReqJumpPage(''),
                    )
                  }
                  className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 ring-1 ring-inset ring-gray-200 hover:bg-gray-50"
                >
                  Go
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
