'use client';

import { useId, useState } from 'react';
import { useRecentRequests } from '@/lib/hooks';
import { InlineErrorText } from '@/components/ui/InlineErrorText';
import type { RecentRequestItem } from '@/lib/api/user';
import { formatRouteWiseDecision } from '@/lib/utils/routewise';

const PAGE_SIZE = 20;

function StatusBadge({ code }: { code: number | null | undefined }) {
  if (code == null) return <span className="text-gray-400">—</span>;
  const isSuccess = code >= 200 && code < 400;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        isSuccess
          ? 'bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20'
          : 'bg-red-50 text-red-700 ring-1 ring-inset ring-red-600/20'
      }`}
    >
      {code}
    </span>
  );
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60000);

  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;

  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;

  const diffDays = Math.floor(diffHr / 24);
  if (diffDays < 7) return `${diffDays}d ago`;

  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatTokens(n: number | null | undefined): string {
  if (n == null) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}

function formatCost(cost: number | null | undefined): string {
  if (cost == null) return '—';
  if (cost < 0.0001) return '<$0.0001';
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

function formatLatency(latencyMs: number | null | undefined): string {
  if (latencyMs == null) return '—';
  if (latencyMs >= 1000) return `${(latencyMs / 1000).toFixed(1)}s`;
  return `${Math.round(latencyMs).toLocaleString()}ms`;
}

function DetailStat({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="rounded-xl bg-white/90 px-3 py-3 shadow-sm ring-1 ring-inset ring-gray-200">
      <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-gray-500">
        {label}
      </div>
      <div className="mt-1 break-words text-sm font-semibold text-gray-900">{value}</div>
    </div>
  );
}

function RequestRow({ req }: { req: RecentRequestItem }) {
  const [expanded, setExpanded] = useState(false);
  const cachedTokens = req.cache_read_tokens ?? null;
  const isEmbedding = req.request_type === 'embedding';
  const routewiseDecision = formatRouteWiseDecision(req);

  const throughputTps =
    req.stream &&
    req.completion_tokens != null &&
    req.completion_tokens > 1 &&
    req.ttft_ms != null &&
    req.latency_ms != null &&
    req.latency_ms > req.ttft_ms
      ? ((req.completion_tokens - 1) * 1000) / (req.latency_ms - req.ttft_ms)
      : null;

  return (
    <>
      <tr
        className="border-b border-gray-100 hover:bg-gray-50/60 cursor-pointer transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <td className="py-3 pl-4 pr-3 text-sm">
          <div className="flex items-center gap-2">
            <span
              className="block max-w-[180px] truncate font-medium text-gray-900 sm:max-w-[260px]"
              title={req.model_id}
            >
              {req.model_id}
            </span>
            {isEmbedding && (
              <span className="inline-flex items-center rounded-md bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 ring-1 ring-inset ring-violet-600/20">
                embedding
              </span>
            )}
            {req.stream && (
              <span className="inline-flex items-center rounded-md bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700 ring-1 ring-inset ring-blue-600/20">
                stream
              </span>
            )}
          </div>
          {req.error && <InlineErrorText message={req.error} />}
        </td>
        <td className="whitespace-nowrap px-3 py-3 text-sm">
          <StatusBadge code={req.status_code} />
        </td>
        <td className="whitespace-nowrap px-3 py-3 text-sm text-gray-600">
          <div className="flex items-center gap-1">
            <span className="text-gray-400">↑</span>
            {formatTokens(req.prompt_tokens)}
            <span className="mx-0.5 text-gray-300">/</span>
            <span className="text-gray-400">↓</span>
            {formatTokens(req.completion_tokens)}
            {req.reasoning_tokens != null && req.reasoning_tokens > 0 && (
              <>
                <span className="mx-0.5 text-gray-300">/</span>
                <span
                  className="inline-flex items-center rounded bg-purple-50 px-1 text-[10px] font-medium text-purple-700"
                  title={`${req.reasoning_tokens.toLocaleString()} reasoning tokens`}
                >
                  R {formatTokens(req.reasoning_tokens)}
                </span>
              </>
            )}
            {cachedTokens != null && cachedTokens > 0 && (
              <>
                <span className="mx-0.5 text-gray-300">/</span>
                <span
                  className="inline-flex items-center rounded bg-amber-50 px-1 text-[10px] font-medium text-amber-700"
                  title={`${cachedTokens.toLocaleString()} cached token${cachedTokens !== 1 ? 's' : ''}`}
                >
                  C {formatTokens(cachedTokens)}
                </span>
              </>
            )}
          </div>
        </td>
        <td className="whitespace-nowrap px-3 py-3 text-sm text-gray-600">
          {formatCost(req.cost_usd)}
        </td>
        <td className="whitespace-nowrap px-3 py-3 text-right text-xs text-gray-500">
          <span title={new Date(req.timestamp).toLocaleString()}>
            {formatTimestamp(req.timestamp)}
          </span>
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-gray-100 bg-gray-50/40">
          <td colSpan={5} className="px-4 py-3">
            <div className="rounded-2xl bg-gradient-to-br from-slate-50 via-white to-gray-50 p-4 shadow-sm ring-1 ring-inset ring-gray-200">
              <div className="flex flex-col gap-3 border-b border-gray-200/80 pb-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-gray-500">
                    Request Details
                  </div>
                  <div className="mt-2 break-all font-mono text-sm text-gray-700">
                    {req.request_id}
                  </div>
                </div>
                <div className="inline-flex w-fit items-center rounded-full bg-white px-2.5 py-1 text-[11px] font-medium text-gray-600 shadow-sm ring-1 ring-inset ring-gray-200">
                  {req.provider}
                </div>
              </div>

              <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <DetailStat label="Model" value={req.model_id} />
                <DetailStat label="Latency" value={formatLatency(req.latency_ms)} />
                <DetailStat label="TTFT" value={formatLatency(req.ttft_ms)} />
                <DetailStat label="Total Tokens" value={formatTokens(req.total_tokens)} />
                <DetailStat label="Cached Tokens" value={formatTokens(cachedTokens)} />
                <DetailStat
                  label="Streaming"
                  value={req.stream != null ? (req.stream ? 'Enabled' : 'Disabled') : '—'}
                />
                <DetailStat
                  label="Prompt / Output"
                  value={`${formatTokens(req.prompt_tokens)} / ${formatTokens(req.completion_tokens)}`}
                />
                {throughputTps != null && (
                  <DetailStat label="Throughput" value={`${throughputTps.toFixed(1)} tok/s`} />
                )}
                {routewiseDecision && <DetailStat label="RouteWise" value={routewiseDecision} />}
              </div>

              {req.error && (
                <div className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700 ring-1 ring-inset ring-red-200">
                  <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-red-500">
                    Request Error
                  </div>
                  <div className="mt-1 break-words">{req.error}</div>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export function RecentRequests(): JSX.Element {
  const [page, setPage] = useState(0);
  const [jumpTo, setJumpTo] = useState('');
  const jumpInputId = useId();
  const offset = page * PAGE_SIZE;
  const { data, isLoading, error } = useRecentRequests(PAGE_SIZE, offset);

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 0;

  const applyJumpToPage = (): void => {
    if (totalPages < 2) return;
    const n = Number.parseInt(jumpTo.trim(), 10);
    if (!Number.isFinite(n)) return;
    const target = Math.min(Math.max(1, n), totalPages);
    setPage(target - 1);
    setJumpTo('');
  };

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-base font-semibold tracking-tight text-gray-900 sm:text-lg">
          Recent Requests
        </h2>
        {data && (
          <span className="text-xs text-gray-500">
            {data.total.toLocaleString()} total request{data.total !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-3 text-red-700 ring-1 ring-inset ring-red-200">
          Failed to load recent requests. Please try again later.
        </div>
      )}

      {isLoading && (
        <div className="flex justify-center py-8">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600" />
        </div>
      )}

      {!isLoading && data && data.requests.length === 0 && (
        <div className="flex flex-col items-center justify-center py-12 text-gray-500">
          <svg
            className="mb-3 h-10 w-10 text-gray-300"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.5}
              d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
            />
          </svg>
          <p className="text-sm">No requests yet</p>
          <p className="mt-1 text-xs">Requests will appear here once you start using the API.</p>
        </div>
      )}

      {!isLoading && data && data.requests.length > 0 && (
        <>
          <div className="-mx-6 overscroll-x-contain overflow-x-auto">
            <table className="min-w-full">
              <thead>
                <tr className="border-b border-gray-200">
                  <th className="py-2.5 pl-10 pr-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                    Model
                  </th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                    Status
                  </th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                    Tokens
                  </th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                    Cost
                  </th>
                  <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500 pr-10">
                    Time
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.requests.map((req) => (
                  <RequestRow key={req.request_id} req={req} />
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="mt-4 flex flex-col gap-3 border-t border-gray-100 pt-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-center justify-center gap-3 sm:justify-start">
                <button
                  onClick={() => setPage(Math.max(0, page - 1))}
                  disabled={page === 0}
                  className="inline-flex items-center rounded-md bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-300 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  ← Previous
                </button>
                <span className="text-xs text-gray-500 tabular-nums">
                  Page {page + 1} of {totalPages}
                </span>
                <button
                  onClick={() => setPage(Math.min(totalPages - 1, page + 1))}
                  disabled={page >= totalPages - 1}
                  className="inline-flex items-center rounded-md bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-300 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Next →
                </button>
              </div>
              <div className="flex items-center justify-center gap-2 sm:justify-end">
                <label htmlFor={jumpInputId} className="text-xs text-gray-500 whitespace-nowrap">
                  Jump to page
                </label>
                <input
                  id={jumpInputId}
                  type="number"
                  min={1}
                  max={totalPages}
                  inputMode="numeric"
                  value={jumpTo}
                  onChange={(e) => setJumpTo(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') applyJumpToPage();
                  }}
                  className="w-16 rounded-md border border-gray-200 px-2 py-1 text-center text-sm text-gray-900 tabular-nums focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                  aria-label="Page number to jump to"
                />
                <button
                  type="button"
                  onClick={applyJumpToPage}
                  className="inline-flex items-center rounded-md bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-300 hover:bg-gray-50"
                >
                  Go
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
