'use client';

import { Fragment, useCallback, useEffect, useId, useRef, useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { useAuth } from '@/components/providers';
import {
  AdminMetricDistribution,
  AdminPerformanceMetricsWindow,
  AdminUser,
  AdminRecentRequestItem,
  AdminRequestMetricsWindow,
  AuditLogEntry,
  BroadcastDetailResponse,
  BroadcastListItem,
  ProviderQuotaResult,
  StatusCounts,
  UserDetail,
  UserSortBy,
  listUsers,
  getUserDetail,
  updateUser,
  approveUser,
  rejectUser,
  deleteUser,
  resumeUser,
  hardDeleteUser,
  regenerateApiKeyAdmin,
  listAuditLog,
  listRecentRequests,
  getRequestMetrics,
  previewBroadcast,
  sendTestBroadcastEmail,
  createBroadcast,
  listBroadcasts,
  getBroadcastDetail,
  cancelBroadcast,
  exportRequests,
  getPerformanceMetrics,
  getProviderQuotas,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AnalyticsTab } from './AnalyticsTab';
import { ProviderPerformanceTab } from './ProviderPerformanceTab';
import { TokenUsageTab } from './TokenUsageTab';

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

function previewText(value: string, maxChars: number = 280): string {
  if (value.length <= maxChars) return value;
  return `${value.slice(0, maxChars)}...`;
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

type ParseResult = { ok: true; value: unknown } | { ok: false };

function tryParseJson(value: string): ParseResult {
  try {
    return { ok: true, value: JSON.parse(value) as unknown };
  } catch {
    return { ok: false };
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

type ToolCall = {
  id?: unknown;
  type?: unknown;
  function?: { name?: unknown; arguments?: unknown };
};

type ChatMessage = {
  role: string;
  content: unknown;
  reasoning_content?: unknown;
  reasoning?: unknown;
  refusal?: unknown;
  tool_calls?: unknown;
  tool_call_id?: unknown;
  name?: unknown;
};

function isChatMessage(v: unknown): v is ChatMessage {
  return (
    isRecord(v) &&
    typeof v.role === 'string' &&
    ('content' in v ||
      'tool_calls' in v ||
      'refusal' in v ||
      'reasoning' in v ||
      'reasoning_content' in v)
  );
}

function isToolCall(v: unknown): v is ToolCall {
  return isRecord(v);
}

function getToolCalls(message: ChatMessage): ToolCall[] {
  if (!Array.isArray(message.tool_calls)) return [];
  return message.tool_calls.filter(isToolCall);
}

function toolCallName(tc: ToolCall): string {
  const fn = tc.function;
  if (isRecord(fn) && typeof fn.name === 'string') return fn.name;
  return '';
}

function toolCallArgs(tc: ToolCall): string {
  const fn = tc.function;
  if (!isRecord(fn)) return '';
  const args = fn.arguments;
  if (typeof args === 'string') {
    const parsed = tryParseJson(args);
    if (parsed.ok) return JSON.stringify(parsed.value, null, 2);
    return args;
  }
  if (args == null) return '';
  return JSON.stringify(args, null, 2);
}

function flattenContent(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === 'string') return part;
        if (isRecord(part)) {
          if (part.type === 'text' && typeof part.text === 'string') return part.text;
          if (typeof part.type === 'string') return `[${part.type}]`;
        }
        return '';
      })
      .filter((s) => s.length > 0)
      .join('\n');
  }
  if (content == null) return '';
  return JSON.stringify(content);
}

function hasMessages(v: unknown): v is Record<string, unknown> & { messages: ChatMessage[] } {
  if (!isRecord(v)) return false;
  const m = v.messages;
  return Array.isArray(m) && m.length > 0 && m.every(isChatMessage);
}

type ChatChoice = { message: ChatMessage; finish_reason?: unknown; index?: unknown };

function isChatChoice(v: unknown): v is ChatChoice {
  return isRecord(v) && isChatMessage(v.message);
}

function hasChoices(v: unknown): v is Record<string, unknown> & { choices: ChatChoice[] } {
  if (!isRecord(v)) return false;
  const c = v.choices;
  return Array.isArray(c) && c.length > 0 && c.every(isChatChoice);
}

const ROLE_BADGE: Record<string, string> = {
  system: 'bg-gray-100 text-gray-700 border-gray-200',
  user: 'bg-blue-50 text-blue-700 border-blue-200',
  assistant: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  tool: 'bg-amber-50 text-amber-700 border-amber-200',
};

function roleBadgeClass(role: string): string {
  return ROLE_BADGE[role] ?? 'bg-gray-100 text-gray-700 border-gray-200';
}

function formatScalar(v: unknown): string {
  if (v == null) return String(v);
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return JSON.stringify(v);
}

function MetaList({ data, skip }: { data: Record<string, unknown>; skip: ReadonlyArray<string> }) {
  const entries = Object.entries(data).filter(([k]) => !skip.includes(k));
  if (entries.length === 0) return null;
  return (
    <dl className="mb-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-[11px]">
      {entries.map(([k, v]) => (
        <Fragment key={k}>
          <dt className="text-gray-500">{k}:</dt>
          <dd className="text-gray-700 break-words font-mono">{formatScalar(v)}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

function MessageBlock({ message }: { message: ChatMessage }) {
  const text = flattenContent(message.content);
  const reasoning = messageReasoning(message);
  const refusal = messageRefusal(message);
  const toolCalls = getToolCalls(message);
  const toolCallId = typeof message.tool_call_id === 'string' ? message.tool_call_id : '';
  const toolName = typeof message.name === 'string' ? message.name : '';
  const showToolMeta = message.role === 'tool' && (toolCallId || toolName);
  const rendered =
    text.length > 0 ||
    reasoning.length > 0 ||
    refusal.length > 0 ||
    toolCalls.length > 0 ||
    showToolMeta;
  return (
    <div className="rounded-md border border-gray-200 bg-white px-2 py-1.5">
      <div className="mb-1 flex items-center gap-2">
        <span
          className={`inline-block rounded border px-1.5 py-px text-[10px] font-medium uppercase tracking-wide ${roleBadgeClass(
            message.role,
          )}`}
        >
          {message.role}
        </span>
      </div>
      {showToolMeta && (
        <div className="mb-1 flex flex-wrap gap-x-3 text-[10px] text-gray-500 font-mono">
          {toolName && (
            <span>
              name: <span className="text-gray-700">{toolName}</span>
            </span>
          )}
          {toolCallId && (
            <span>
              tool_call_id: <span className="text-gray-700">{toolCallId}</span>
            </span>
          )}
        </div>
      )}
      {text.length > 0 && (
        <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] text-gray-700">
          {text}
        </pre>
      )}
      {reasoning.length > 0 && (
        <div className="mt-1 rounded-md border border-gray-200 bg-white px-2 py-1">
          <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-gray-500">
            reasoning
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] italic text-gray-500">
            {reasoning}
          </pre>
        </div>
      )}
      {refusal.length > 0 && (
        <div className="mt-1 rounded-md border border-red-200 bg-white px-2 py-1">
          <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-red-600">
            refusal
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] text-red-700">
            {refusal}
          </pre>
        </div>
      )}
      {toolCalls.length > 0 && (
        <div className="mt-1 space-y-1">
          {toolCalls.map((tc, i) => {
            const name = toolCallName(tc);
            const args = toolCallArgs(tc);
            const id = typeof tc.id === 'string' ? tc.id : '';
            return (
              <div
                key={id || `${i}-${name}`}
                className="rounded-md border border-gray-200 bg-white px-2 py-1"
              >
                <div className="mb-0.5 flex flex-wrap items-center gap-x-2 text-[10px]">
                  <span className="font-medium uppercase tracking-wide text-gray-500">
                    tool_call
                  </span>
                  {name && <span className="font-mono text-gray-700">{name}</span>}
                  {id && <span className="font-mono text-gray-400">{id}</span>}
                </div>
                {args.length > 0 && (
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] text-gray-700">
                    {args}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
      {!rendered && <div className="text-[11px] italic text-gray-400">(no content)</div>}
    </div>
  );
}

function JsonChatView({ data }: { data: unknown }) {
  if (hasMessages(data)) {
    return (
      <div className="mt-1 rounded-md border border-gray-200 bg-white px-3 py-2">
        <MetaList data={data} skip={['messages']} />
        <div className="space-y-1.5">
          {data.messages.map((m, i) => (
            <MessageBlock key={`${i}-${m.role}`} message={m} />
          ))}
        </div>
      </div>
    );
  }
  if (hasChoices(data)) {
    return (
      <div className="mt-1 rounded-md border border-gray-200 bg-white px-3 py-2">
        <MetaList data={data} skip={['choices']} />
        <div className="space-y-1.5">
          {data.choices.map((c, i) => (
            <MessageBlock key={typeof c.index === 'number' ? c.index : i} message={c.message} />
          ))}
        </div>
      </div>
    );
  }
  return (
    <pre className="mt-1 overflow-x-auto rounded-md border border-gray-200 bg-white px-3 py-2 text-[11px] text-gray-700 whitespace-pre-wrap break-words">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

function toolCallsPreview(message: ChatMessage): string {
  const calls = getToolCalls(message);
  if (calls.length === 0) return '';
  const names = calls.map(toolCallName).filter((n) => n.length > 0);
  return names.length > 0 ? `[tool_calls: ${names.join(', ')}]` : '[tool_calls]';
}

function messageRefusal(m: ChatMessage): string {
  return typeof m.refusal === 'string' ? m.refusal : '';
}

function messageReasoning(m: ChatMessage): string {
  if (typeof m.reasoning_content === 'string') return m.reasoning_content;
  if (typeof m.reasoning === 'string') return m.reasoning;
  return '';
}

function computePreview(parsed: unknown, fallback: string): string {
  if (hasMessages(parsed)) {
    for (let i = parsed.messages.length - 1; i >= 0; i--) {
      const m = parsed.messages[i];
      if (m.role === 'user') {
        const text = flattenContent(m.content);
        if (text) return previewText(text);
      }
    }
    for (let i = parsed.messages.length - 1; i >= 0; i--) {
      const m = parsed.messages[i];
      if (m.role === 'assistant') {
        const text = flattenContent(m.content);
        if (text) return previewText(text);
        const tc = toolCallsPreview(m);
        if (tc) return previewText(tc);
        const refusal = messageRefusal(m);
        if (refusal) return previewText(`[refusal] ${refusal}`);
        const reasoning = messageReasoning(m);
        if (reasoning) return previewText(`[reasoning] ${reasoning}`);
        break;
      }
    }
    const last = parsed.messages[parsed.messages.length - 1];
    if (last) {
      const text = flattenContent(last.content);
      if (text) return previewText(text);
      const tc = toolCallsPreview(last);
      if (tc) return previewText(tc);
      const refusal = messageRefusal(last);
      if (refusal) return previewText(`[refusal] ${refusal}`);
      const reasoning = messageReasoning(last);
      if (reasoning) return previewText(`[reasoning] ${reasoning}`);
    }
  }
  if (hasChoices(parsed)) {
    const first = parsed.choices[0];
    const m = first.message;
    const text = flattenContent(m.content);
    if (text) return previewText(text);
    const tc = toolCallsPreview(m);
    if (tc) return previewText(tc);
    const refusal = messageRefusal(m);
    if (refusal) return previewText(`[refusal] ${refusal}`);
    const reasoning = messageReasoning(m);
    if (reasoning) return previewText(`[reasoning] ${reasoning}`);
  }
  return previewText(fallback);
}

function FoldedText({ label, value }: { label: string; value?: string | null }) {
  const [open, setOpen] = useState(false);
  if (!value) {
    return (
      <div className="col-span-full">
        <span className="text-gray-500">{label}:</span> <span className="text-gray-700">—</span>
      </div>
    );
  }

  const parsed = tryParseJson(value);
  const preview = parsed.ok ? computePreview(parsed.value, value) : previewText(value);

  return (
    <details
      className="col-span-full group"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer list-none text-gray-500 flex items-center gap-2">
        <span>{label}:</span>
        <span className="text-gray-700 whitespace-pre-wrap break-words">{preview}</span>
        <span className="text-[10px] text-gray-400 group-open:hidden">(show more)</span>
        <span className="text-[10px] text-gray-400 hidden group-open:inline">(show less)</span>
      </summary>
      {open &&
        (parsed.ok ? (
          <JsonChatView data={parsed.value} />
        ) : (
          <pre className="mt-1 overflow-x-auto rounded-md border border-gray-200 bg-white px-3 py-2 text-[11px] text-gray-700 whitespace-pre-wrap break-words">
            {value}
          </pre>
        ))}
    </details>
  );
}

function formatLatency(ms?: number | null): string {
  if (ms == null) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
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

function formatTokens(n: number): string {
  return Math.round(n).toLocaleString();
}

function formatBucketEdge(value: number, kind: 'tokens' | 'ms'): string {
  if (kind === 'tokens') {
    if (value >= 1000) return `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}k`;
    return value.toLocaleString();
  }
  if (value >= 1000) return `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}s`;
  return `${value}ms`;
}

function PerformanceMetricsCard({ metric }: { metric: AdminPerformanceMetricsWindow }) {
  const rows: Array<{
    title: string;
    dist: AdminMetricDistribution;
    kind: 'tokens' | 'ms';
  }> = [
    { title: 'Prompt tokens', dist: metric.prompt_tokens, kind: 'tokens' },
    { title: 'Response tokens', dist: metric.completion_tokens, kind: 'tokens' },
    { title: 'TTFT', dist: metric.ttft_ms, kind: 'ms' },
    { title: 'TBT', dist: metric.tbt_ms, kind: 'ms' },
  ];
  const formatValue = (v: number | null | undefined, kind: 'tokens' | 'ms'): string => {
    if (v == null) return '—';
    return kind === 'ms' ? formatLatency(v) : formatTokens(v);
  };
  return (
    <div className="rounded-xl border border-gray-200 bg-white px-3 py-2.5 shadow-sm">
      <div className="flex items-center justify-between">
        <div className="text-[12px] font-semibold text-gray-900">{metric.label}</div>
        <div className="text-[10px] text-gray-400">{metric.window_minutes}m window</div>
      </div>
      <table className="mt-2 w-full">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-gray-400 font-medium">
            <th className="py-1 text-left">Metric</th>
            <th className="py-1 text-right">n</th>
            <th className="py-1 text-right">p50</th>
            <th className="py-1 text-right">p95</th>
            <th className="py-1 text-right">p99</th>
            <th className="py-1 text-right">Dist</th>
          </tr>
        </thead>
        <tbody className="[&>tr+tr>td]:border-t [&>tr+tr>td]:border-gray-100">
          {rows.map((row) => {
            const maxBucket = Math.max(...row.dist.histogram.map((b) => b.count), 1);
            return (
              <tr key={row.title}>
                <td className="py-1.5 text-[11px] text-gray-600">{row.title}</td>
                <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                  {row.dist.count.toLocaleString()}
                </td>
                <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                  {formatValue(row.dist.p50, row.kind)}
                </td>
                <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                  {formatValue(row.dist.p95, row.kind)}
                </td>
                <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                  {formatValue(row.dist.p99, row.kind)}
                </td>
                <td className="py-1.5 text-right">
                  {(() => {
                    const peakIdx = row.dist.histogram.reduce(
                      (best, b, i, arr) => (b.count > arr[best].count ? i : best),
                      0,
                    );
                    const peak = row.dist.histogram[peakIdx];
                    const peakLower = peak ? formatBucketEdge(peak.lower_bound, row.kind) : '';
                    const peakUpper =
                      peak == null
                        ? ''
                        : peak.upper_bound == null
                          ? '∞'
                          : formatBucketEdge(peak.upper_bound, row.kind);
                    const srSummary =
                      peak && peak.count > 0
                        ? `Distribution peak [${peakLower}, ${peakUpper}) with ${peak.count.toLocaleString()} samples across ${row.dist.histogram.length} buckets`
                        : 'Distribution: no samples';
                    return (
                      <>
                        <span className="sr-only">{srSummary}</span>
                        <div aria-hidden="true" className="ml-auto flex h-5 w-20 items-end gap-px">
                          {row.dist.histogram.map((b, idx) => {
                            const height = b.count === 0 ? 2 : (b.count / maxBucket) * 100;
                            const upperLabel =
                              b.upper_bound == null
                                ? '∞'
                                : formatBucketEdge(b.upper_bound, row.kind);
                            const lowerLabel = formatBucketEdge(b.lower_bound, row.kind);
                            return (
                              <div
                                key={`${idx}-${b.lower_bound}`}
                                className={`min-w-0 flex-1 rounded-t-[1px] ${
                                  b.count === 0 ? 'bg-gray-200' : 'bg-gray-700'
                                }`}
                                style={{ height: `${height}%` }}
                                title={`[${lowerLabel}, ${upperLabel}): ${b.count.toLocaleString()}`}
                              />
                            );
                          })}
                        </div>
                      </>
                    );
                  })()}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function pct(used: number | null, limit: number | null): number | null {
  if (used == null || limit == null || limit <= 0) return null;
  return Math.min(100, (used / limit) * 100);
}

function formatNum(v: number | null): string {
  if (v == null) return '—';
  if (Math.abs(v) < 0.01 && v !== 0) return v.toFixed(4);
  if (Math.abs(v) < 1 && v !== 0) return v.toFixed(1);
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toFixed(2);
}

function ProviderCard({ provider }: { provider: ProviderQuotaResult }) {
  const stripeColor = provider.ok
    ? 'bg-emerald-500'
    : provider.error === 'not_configured'
      ? 'bg-gray-300'
      : 'bg-red-400';

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
      <div className={`h-1 ${stripeColor}`} />
      <div className="p-4">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="text-[15px] font-semibold text-gray-900">{provider.display_name}</h3>
          <span
            className={`tabular-nums text-[11px] ${provider.key_configured ? 'text-gray-500' : 'text-gray-400'}`}
          >
            {provider.key_masked ?? 'Not configured'}
          </span>
        </div>

        {provider.ok ? (
          provider.usages.length === 0 ? (
            <p className="mt-3 text-[12px] text-gray-400">No usage data returned.</p>
          ) : (
            <div className="mt-3 space-y-3">
              {provider.usages.map((u, i) => {
                const p = pct(u.used, u.limit);
                return (
                  <div key={i}>
                    <div className="flex items-baseline justify-between text-[12px]">
                      <span className="text-gray-600">{u.label}</span>
                      <span className="tabular-nums text-gray-700">
                        {formatNum(u.used)}
                        {u.limit != null && ` / ${formatNum(u.limit)}`} {u.unit}
                        {p != null && <span className="ml-1 text-gray-400">({p.toFixed(0)}%)</span>}
                      </span>
                    </div>
                    {p != null && (
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-gray-100">
                        <div
                          className={`h-full ${
                            p >= 90 ? 'bg-red-400' : p >= 70 ? 'bg-amber-400' : 'bg-gray-900'
                          }`}
                          style={{ width: `${p}%` }}
                        />
                      </div>
                    )}
                    {u.reset_at && (
                      <p className="mt-1 text-[11px] text-gray-400">
                        Resets at{' '}
                        {new Date(u.reset_at).toLocaleString('en-US', {
                          year: 'numeric',
                          month: 'short',
                          day: 'numeric',
                          hour: 'numeric',
                          minute: '2-digit',
                          timeZoneName: 'short',
                        })}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )
        ) : (
          <p className="mt-3 text-[12px] text-gray-400">
            Quota unavailable — <span className="text-gray-500">{provider.error}</span>
          </p>
        )}
      </div>
    </div>
  );
}

const AUDIT_ACTIONS = [
  'create_user',
  'approve_user',
  'reject_user',
  'update_user',
  'delete_user',
  'resume_user',
  'hard_delete_user',
  'create_key',
  'revoke_key',
  'delete_key',
  'hard_delete_key',
  'regenerate_key',
  'update_key',
];

function actionLabel(action: string): string {
  if (!action) return action;
  const s = action.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

type AuditCategory = 'create' | 'approve' | 'reject' | 'update' | 'delete' | 'other';

function actionCategory(action: string): AuditCategory {
  if (action === 'regenerate_key') return 'create';
  if (
    action.startsWith('hard_delete') ||
    action.startsWith('revoke_') ||
    action.endsWith('_revoke')
  )
    return 'delete';
  if (action.startsWith('approve_') || action.endsWith('_approve')) return 'approve';
  if (action.startsWith('reject_') || action.endsWith('_reject') || action.endsWith('_cancel'))
    return 'reject';
  if (action.startsWith('create_') || action.endsWith('_create')) return 'create';
  if (action.startsWith('update_') || action.endsWith('_update')) return 'update';
  if (action.startsWith('delete_') || action.endsWith('_delete')) return 'delete';
  return 'other';
}

const AUDIT_CATEGORY_CLASS: Record<AuditCategory, string> = {
  create: 'bg-emerald-50 text-emerald-700',
  approve: 'bg-violet-50 text-violet-700',
  reject: 'bg-amber-50 text-amber-700',
  update: 'bg-sky-50 text-sky-700',
  delete: 'bg-rose-50 text-rose-700',
  other: 'bg-gray-100 text-gray-700',
};

function formatRelative(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${Math.max(s, 0)}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function formatAuditDetailValue(v: unknown): { display: string; full: string } {
  if (v === null || v === undefined) return { display: '—', full: '—' };
  if (typeof v === 'string') {
    const full = v;
    const display = v.length > 80 ? `${v.slice(0, 80)}…` : v;
    return { display, full };
  }
  if (typeof v === 'number' || typeof v === 'boolean') {
    const s = String(v);
    return { display: s, full: s };
  }
  const full = JSON.stringify(v);
  const display = full.length > 80 ? `${full.slice(0, 80)}…` : full;
  return { display, full };
}

function RawJsonDetails({ data }: { data: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="text-[11px] text-gray-400 cursor-pointer hover:text-gray-600 mt-1">
        Raw JSON
      </summary>
      {open && (
        <pre className="mt-1.5 rounded-md bg-gray-50 px-3 py-2 text-[11px] text-gray-600 overflow-x-auto border border-gray-100">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </details>
  );
}

export default function AdminPage() {
  const { state } = useAuth();

  // Top-level tab
  const [activeTab, setActiveTab] = useState<
    | 'users'
    | 'audit'
    | 'requests'
    | 'broadcast'
    | 'providers'
    | 'provider-perf'
    | 'analytics'
    | 'performance'
    | 'token-usage'
  >('users');

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get('tab');
    if (
      tab === 'users' ||
      tab === 'audit' ||
      tab === 'requests' ||
      tab === 'broadcast' ||
      tab === 'providers' ||
      tab === 'provider-perf' ||
      tab === 'analytics' ||
      tab === 'performance' ||
      tab === 'token-usage'
    ) {
      setActiveTab(
        tab as
          | 'users'
          | 'audit'
          | 'requests'
          | 'broadcast'
          | 'providers'
          | 'provider-perf'
          | 'analytics'
          | 'performance'
          | 'token-usage',
      );
    }
  }, []);

  // Users state
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [counts, setCounts] = useState<StatusCounts>({
    all: 0,
    pending_approval: 0,
    active: 0,
    suspended: 0,
    rejected: 0,
    deleted: 0,
  });
  const [filter, setFilter] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const [sortBy, setSortBy] = useState<UserSortBy>('created');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [rejectTarget, setRejectTarget] = useState<AdminUser | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);
  const [deleteReason, setDeleteReason] = useState('');
  const [hardDeleteTarget, setHardDeleteTarget] = useState<AdminUser | null>(null);
  const [hardDeleteReason, setHardDeleteReason] = useState('');
  const [hardDeleteEmailConfirm, setHardDeleteEmailConfirm] = useState('');
  const [newKey, setNewKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editRole, setEditRole] = useState('');
  const [editQuota, setEditQuota] = useState('');
  const [saving, setSaving] = useState(false);

  // Broadcast email state
  const [broadcasts, setBroadcasts] = useState<BroadcastListItem[]>([]);
  const [broadcastLoading, setBroadcastLoading] = useState(false);
  const [broadcastDetail, setBroadcastDetail] = useState<BroadcastDetailResponse | null>(null);
  const [broadcastDetailLoading, setBroadcastDetailLoading] = useState(false);
  const [bcTemplateKey, setBcTemplateKey] = useState<string>('custom');
  const [bcTemplateVars, setBcTemplateVars] = useState<Record<string, string>>({});
  const [bcSubject, setBcSubject] = useState('');
  const [bcBodyHtml, setBcBodyHtml] = useState('');
  const [bcScheduleMode, setBcScheduleMode] = useState<'now' | 'later'>('now');
  const [bcScheduledAt, setBcScheduledAt] = useState('');
  const [bcPreview, setBcPreview] = useState<{
    recipient_count: number;
    rendered_subject: string;
    rendered_body_html: string;
  } | null>(null);
  const [bcPreviewLoading, setBcPreviewLoading] = useState(false);
  const [bcSending, setBcSending] = useState(false);
  const [bcConfirm, setBcConfirm] = useState(false);
  const [bcTestLoading, setBcTestLoading] = useState(false);
  const [bcTargetRoles, setBcTargetRoles] = useState<string[]>(['free', 'internal', 'admin']);
  const [bcTargetStatuses, setBcTargetStatuses] = useState<string[]>(['active']);

  // Audit log state
  const [auditEntries, setAuditEntries] = useState<AuditLogEntry[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditFilter, setAuditFilter] = useState('');
  const [auditUserFilter, setAuditUserFilter] = useState('');
  const [auditOffset, setAuditOffset] = useState(0);
  const auditReqIdRef = useRef(0);
  const AUDIT_PAGE_SIZE = 50;

  // Requests state
  const [reqEntries, setReqEntries] = useState<AdminRecentRequestItem[]>([]);
  const [reqTotal, setReqTotal] = useState(0);
  const [reqLoading, setReqLoading] = useState(false);
  const [reqOffset, setReqOffset] = useState(0);
  const [reqUserFilter, setReqUserFilter] = useState('');
  const [reqModelFilter, setReqModelFilter] = useState('');
  const [reqErrorsOnly, setReqErrorsOnly] = useState(false);
  const [reqExpandedId, setReqExpandedId] = useState<string | null>(null);
  const [reqJumpPage, setReqJumpPage] = useState('');
  const [reqMetrics, setReqMetrics] = useState<AdminRequestMetricsWindow[]>([]);
  const [reqMetricsLoading, setReqMetricsLoading] = useState(false);
  const [perfMetrics, setPerfMetrics] = useState<AdminPerformanceMetricsWindow[]>([]);
  const [perfMetricsLoading, setPerfMetricsLoading] = useState(false);
  const reqJumpInputId = useId();
  const REQ_PAGE_SIZE = 50;
  const [showExportPanel, setShowExportPanel] = useState(false);
  const [exportStartDate, setExportStartDate] = useState('');
  const [exportEndDate, setExportEndDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [exportIncludeContent, setExportIncludeContent] = useState(false);
  const [exportLoading, setExportLoading] = useState(false);

  // Providers state
  const [providerQuotas, setProviderQuotas] = useState<ProviderQuotaResult[]>([]);
  const [providerQuotasLoading, setProviderQuotasLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await listUsers(
        filter || undefined,
        100,
        0,
        searchTerm || undefined,
        sortBy !== 'created' ? sortBy : undefined,
      );
      setUsers(d.users);
      setCounts(d.status_counts);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [filter, searchTerm, sortBy]);

  const loadAudit = useCallback(async () => {
    const reqId = ++auditReqIdRef.current;
    setAuditLoading(true);
    setError(null);
    try {
      const d = await listAuditLog(
        auditFilter || undefined,
        auditUserFilter || undefined,
        AUDIT_PAGE_SIZE,
        auditOffset,
      );
      if (auditReqIdRef.current !== reqId) return;
      setAuditEntries(d.entries);
      setAuditTotal(d.total);
    } catch (e) {
      if (auditReqIdRef.current !== reqId) return;
      setError(getErrorMessage(e));
    } finally {
      if (auditReqIdRef.current === reqId) setAuditLoading(false);
    }
  }, [auditFilter, auditUserFilter, auditOffset]);

  const loadRequests = useCallback(async () => {
    setReqLoading(true);
    setError(null);
    try {
      const d = await listRecentRequests(
        REQ_PAGE_SIZE,
        reqOffset,
        reqUserFilter || undefined,
        reqModelFilter || undefined,
        reqErrorsOnly,
      );
      setReqEntries(d.requests);
      setReqTotal(d.total);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setReqLoading(false);
    }
  }, [reqOffset, reqUserFilter, reqModelFilter, reqErrorsOnly]);

  const loadRequestMetrics = useCallback(async () => {
    setReqMetricsLoading(true);
    setError(null);
    try {
      const d = await getRequestMetrics();
      setReqMetrics(d.windows);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setReqMetricsLoading(false);
    }
  }, []);

  const loadPerformanceMetrics = useCallback(async () => {
    setPerfMetricsLoading(true);
    setError(null);
    try {
      const d = await getPerformanceMetrics();
      setPerfMetrics(d.windows);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setPerfMetricsLoading(false);
    }
  }, []);

  const loadProviderQuotas = useCallback(async () => {
    setProviderQuotasLoading(true);
    setError(null);
    try {
      const d = await getProviderQuotas();
      setProviderQuotas(d.providers);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setProviderQuotasLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === 'users') load();
  }, [load, activeTab]);

  useEffect(() => {
    if (activeTab === 'audit') loadAudit();
  }, [loadAudit, activeTab]);

  useEffect(() => {
    if (activeTab === 'requests') {
      loadRequests();
      loadRequestMetrics();
    }
  }, [loadRequests, loadRequestMetrics, activeTab]);

  useEffect(() => {
    if (activeTab === 'performance') loadPerformanceMetrics();
  }, [loadPerformanceMetrics, activeTab]);

  useEffect(() => {
    if (activeTab === 'providers') loadProviderQuotas();
  }, [loadProviderQuotas, activeTab]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  const loadBroadcasts = useCallback(async () => {
    setBroadcastLoading(true);
    try {
      const res = await listBroadcasts(50, 0);
      setBroadcasts(res.broadcasts);
    } catch {
      // non-fatal
    } finally {
      setBroadcastLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === 'broadcast') loadBroadcasts();
  }, [loadBroadcasts, activeTab]);

  const toggleDetail = async (uid: string) => {
    if (expandedId === uid) {
      setExpandedId(null);
      setDetail(null);
      return;
    }
    setExpandedId(uid);
    setDetailLoading(true);
    setDetail(null);
    try {
      const d = await getUserDetail(uid);
      setDetail(d);
      setEditRole(d.role || 'free');
      setEditQuota(d.quota_daily_usd?.toString() || '100');
    } catch (e) {
      setError(getErrorMessage(e));
      setExpandedId(null);
    } finally {
      setDetailLoading(false);
    }
  };

  const act = async (fn: () => Promise<void>) => {
    try {
      await fn();
    } catch (e) {
      setError(getErrorMessage(e));
    }
  };
  const doApprove = (u: AdminUser) => {
    setBusy(u.id);
    act(async () => {
      await approveUser(u.id);
      setToast(`Approved ${u.email}`);
      await load();
    }).finally(() => setBusy(null));
  };
  const doReject = () => {
    if (!rejectTarget || !rejectReason.trim()) return;
    setBusy(rejectTarget.id);
    act(async () => {
      await rejectUser(rejectTarget.id, rejectReason.trim());
      setToast(`Rejected ${rejectTarget.email}`);
      setRejectTarget(null);
      setRejectReason('');
      await load();
    }).finally(() => setBusy(null));
  };
  const doDelete = () => {
    if (!deleteTarget || !deleteReason.trim()) return;
    setBusy(deleteTarget.id);
    act(async () => {
      await deleteUser(deleteTarget.id, deleteReason.trim());
      setToast(`Deleted ${deleteTarget.email}`);
      setDeleteTarget(null);
      setDeleteReason('');
      setExpandedId(null);
      setDetail(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doResume = (u: AdminUser) => {
    setBusy(u.id);
    act(async () => {
      await resumeUser(u.id);
      setToast(`Resumed ${u.email}`);
      setExpandedId(null);
      setDetail(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doHardDelete = () => {
    if (!hardDeleteTarget) return;
    if (hardDeleteEmailConfirm !== hardDeleteTarget.email) return;
    setBusy(hardDeleteTarget.id);
    const email = hardDeleteTarget.email;
    act(async () => {
      await hardDeleteUser(hardDeleteTarget.id, hardDeleteReason.trim() || undefined);
      setToast(`Permanently deleted ${email}`);
      setHardDeleteTarget(null);
      setHardDeleteReason('');
      setHardDeleteEmailConfirm('');
      setExpandedId(null);
      setDetail(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doRegen = (u: AdminUser) => {
    if (!confirm(`Regenerate key for ${u.email}?`)) return;
    setBusy(u.id);
    act(async () => {
      const r = await regenerateApiKeyAdmin(u.id);
      setNewKey(r.api_key);
      await load();
    }).finally(() => setBusy(null));
  };
  const doSuspend = (uid: string) => {
    if (!confirm('Suspend this user?')) return;
    setBusy(uid);
    act(async () => {
      await updateUser(uid, { status: 'suspended' });
      setToast('Suspended');
      setExpandedId(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doReactivate = (uid: string) => {
    setBusy(uid);
    act(async () => {
      await updateUser(uid, { status: 'active' });
      setToast('Reactivated');
      setExpandedId(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doSave = () => {
    if (!expandedId || !detail) return;
    setSaving(true);
    act(async () => {
      const u: Record<string, unknown> = {};
      if (editRole !== (detail.role || 'free')) u.role = editRole;
      if (editQuota !== (detail.quota_daily_usd?.toString() || '100'))
        u.quota_daily_cost_usd = Number(editQuota);
      if (!Object.keys(u).length) return;
      await updateUser(expandedId, u);
      setToast('Saved');
      const d = await getUserDetail(expandedId);
      setDetail(d);
      await load();
    }).finally(() => setSaving(false));
  };

  if (!state.user?.is_admin) {
    return (
      <ProtectedRoute>
        <div className="flex min-h-[50vh] flex-col items-center justify-center text-center">
          <h1 className="text-lg font-semibold text-gray-900">Admin access required</h1>
          <p className="mt-1 text-sm text-gray-500">
            You don&apos;t have permission to view this page.
          </p>
          <a
            href="/dashboard"
            className="mt-5 text-sm font-medium text-gray-900 underline decoration-gray-300 underline-offset-4 hover:decoration-gray-900 transition"
          >
            Back to Dashboard
          </a>
        </div>
      </ProtectedRoute>
    );
  }

  const filters = [
    { key: '', label: 'All', count: counts.all },
    { key: 'pending_approval', label: 'Pending', count: counts.pending_approval },
    { key: 'active', label: 'Active', count: counts.active },
    { key: 'rejected', label: 'Rejected', count: counts.rejected },
    { key: 'suspended', label: 'Suspended', count: counts.suspended },
    { key: 'deleted', label: 'Deleted', count: counts.deleted },
  ];

  const onTabChange = (
    tab:
      | 'users'
      | 'audit'
      | 'requests'
      | 'broadcast'
      | 'providers'
      | 'provider-perf'
      | 'analytics'
      | 'performance'
      | 'token-usage',
  ) => {
    setActiveTab(tab);
    const params = new URLSearchParams(window.location.search);
    params.set('tab', tab);
    const next = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState({}, '', next);
  };

  const refreshActiveTab = () => {
    if (activeTab === 'users') {
      load();
      return;
    }
    if (activeTab === 'audit') {
      loadAudit();
      return;
    }
    if (activeTab === 'broadcast') {
      loadBroadcasts();
      return;
    }
    if (activeTab === 'providers') {
      loadProviderQuotas();
      return;
    }
    if (activeTab === 'performance') {
      loadPerformanceMetrics();
      return;
    }
    if (activeTab === 'analytics') {
      return;
    }
    if (activeTab === 'provider-perf') {
      return;
    }
    if (activeTab === 'token-usage') {
      return;
    }
    loadRequests();
    loadRequestMetrics();
  };

  return (
    <ProtectedRoute>
      <div className="mx-auto w-full max-w-4xl pb-20">
        {/* Nav */}
        <div className="mb-10 flex items-center justify-between">
          <a
            href="/dashboard"
            className="group flex items-center gap-1.5 text-[13px] text-gray-400 transition hover:text-gray-900"
          >
            <svg
              className="h-3.5 w-3.5 transition group-hover:-translate-x-px"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18"
              />
            </svg>
            Dashboard
          </a>
          <button
            onClick={refreshActiveTab}
            disabled={
              loading ||
              auditLoading ||
              reqLoading ||
              reqMetricsLoading ||
              perfMetricsLoading ||
              providerQuotasLoading
            }
            className="text-[13px] text-gray-400 transition hover:text-gray-900 disabled:opacity-40"
          >
            {loading ||
            auditLoading ||
            reqLoading ||
            reqMetricsLoading ||
            perfMetricsLoading ||
            providerQuotasLoading
              ? 'Loading...'
              : 'Refresh'}
          </button>
        </div>

        {/* Title */}
        <h1 className="text-[28px] font-bold tracking-tight text-gray-900">Admin</h1>
        <p className="mt-0.5 text-[15px] text-gray-500">
          Manage users, API keys, quotas, and audit log.
        </p>

        {/* Top-level tab toggle */}
        <div className="mt-6 flex items-center gap-1">
          {(
            [
              'users',
              'requests',
              'providers',
              'provider-perf',
              'token-usage',
              'audit',
              'broadcast',
              'analytics',
              'performance',
            ] as const
          ).map((tab) => (
            <button
              key={tab}
              onClick={() => onTabChange(tab)}
              className={`rounded-md px-3.5 py-1.5 text-[13px] font-medium transition ${
                activeTab === tab
                  ? 'bg-gray-900 text-white'
                  : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
              }`}
            >
              {tab === 'users'
                ? 'Users'
                : tab === 'requests'
                  ? 'Recent Requests'
                  : tab === 'providers'
                    ? 'Providers'
                    : tab === 'provider-perf'
                      ? 'Provider Performance'
                      : tab === 'token-usage'
                        ? 'Token Usage'
                        : tab === 'audit'
                          ? 'Audit Log'
                          : tab === 'broadcast'
                            ? 'Broadcast Email'
                            : tab === 'analytics'
                              ? 'Analytics'
                              : 'Performance'}
            </button>
          ))}
        </div>

        {/* Alerts */}
        {error && (
          <div className="mt-4 rounded-lg bg-red-50 px-4 py-2.5 text-[13px] text-red-600">
            {error}{' '}
            <button onClick={() => setError(null)} className="ml-2 font-bold">
              &times;
            </button>
          </div>
        )}
        {toast && (
          <div className="mt-4 rounded-lg bg-gray-900 px-4 py-2.5 text-[13px] text-white">
            {toast}
          </div>
        )}

        {/* ========== Users Tab ========== */}
        {activeTab === 'users' && (
          <>
            {/* Search + Sort */}
            <div className="mt-6 flex items-center gap-3">
              <input
                type="text"
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                placeholder="Search by email or name..."
                className="flex-1 rounded-lg border border-gray-200 bg-white px-4 py-2 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
              />
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as UserSortBy)}
                className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-700 focus:border-gray-400 focus:outline-none"
              >
                <option value="created">Created</option>
                <option value="cost_today">Cost today</option>
                <option value="cost_month">Cost this month</option>
                <option value="cost_alltime">Cost all-time</option>
                <option value="last_login">Recent login</option>
              </select>
            </div>

            {/* Filter tabs */}
            <div className="mt-4 flex items-center gap-1 border-b border-gray-200">
              {filters.map((f) => (
                <button
                  key={f.key}
                  onClick={() => setFilter(f.key)}
                  className={`relative px-3 pb-2.5 pt-1 text-[13px] font-medium transition ${
                    filter === f.key ? 'text-gray-900' : 'text-gray-500 hover:text-gray-800'
                  }`}
                >
                  {f.label}
                  {f.count !== undefined && (
                    <span
                      className={`ml-1 tabular-nums text-[11px] ${
                        filter === f.key ? 'text-gray-500' : 'text-gray-400'
                      }`}
                    >
                      {f.count}
                    </span>
                  )}
                  {filter === f.key && (
                    <span className="absolute inset-x-0 bottom-0 h-[2px] bg-gray-900 rounded-full" />
                  )}
                </button>
              ))}
            </div>

            {/* New key */}
            {newKey && (
              <div className="mt-4 rounded-lg border border-gray-200 bg-white p-4">
                <div className="flex items-center justify-between">
                  <span className="text-[13px] font-semibold text-gray-900">
                    New API key generated
                  </span>
                  <button
                    onClick={() => setNewKey(null)}
                    className="text-gray-300 hover:text-gray-500"
                  >
                    &times;
                  </button>
                </div>
                <p className="mt-1 text-[12px] text-gray-400">
                  Copy it now. It won&apos;t be shown again.
                </p>
                <div className="mt-3 flex items-center gap-2">
                  <code className="flex-1 rounded-md bg-gray-50 px-3 py-2 font-mono text-[13px] text-gray-900 break-all select-all border border-gray-100">
                    {newKey}
                  </code>
                  <button
                    onClick={() => {
                      navigator.clipboard.writeText(newKey);
                      setCopied(true);
                      setTimeout(() => setCopied(false), 2000);
                    }}
                    className="shrink-0 rounded-md bg-gray-900 px-3 py-2 text-[12px] font-semibold text-white hover:bg-gray-800 transition"
                  >
                    {copied ? 'Copied' : 'Copy'}
                  </button>
                </div>
              </div>
            )}

            {/* List */}
            <div className="mt-6">
              {loading ? (
                <div className="flex justify-center py-24">
                  <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                </div>
              ) : users.length === 0 ? (
                <div className="py-24 text-center">
                  <p className="text-[13px] text-gray-400">
                    {filter || searchTerm ? 'No users match this filter.' : 'No users yet.'}
                  </p>
                </div>
              ) : (
                <div>
                  {users.map((u, i) => {
                    const isOpen = expandedId === u.id;
                    return (
                      <div key={u.id}>
                        {/* Row */}
                        <div
                          onClick={() => toggleDetail(u.id)}
                          className={`group flex cursor-pointer items-center gap-4 py-3.5 transition ${
                            i > 0 ? 'border-t border-gray-100' : ''
                          } ${isOpen ? 'opacity-100' : 'hover:bg-gray-50/50'}`}
                          style={{ paddingLeft: 4, paddingRight: 4 }}
                        >
                          {/* Avatar */}
                          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gray-900 text-[11px] font-bold text-white">
                            {(u.user_name || u.email).charAt(0).toUpperCase()}
                          </div>

                          {/* Main */}
                          <div className="min-w-0 flex-1">
                            <div className="flex items-baseline gap-2">
                              <span className="truncate text-[14px] font-medium text-gray-900">
                                {u.email}
                              </span>
                              {u.status === 'pending_approval' && (
                                <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold text-amber-700">
                                  PENDING
                                </span>
                              )}
                              {u.status === 'suspended' && (
                                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-bold text-gray-500">
                                  SUSPENDED
                                </span>
                              )}
                              {u.status === 'rejected' && (
                                <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-bold text-red-500">
                                  REJECTED
                                </span>
                              )}
                              {u.status === 'deleted' && (
                                <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-bold text-gray-600">
                                  DELETED
                                </span>
                              )}
                              {u.role && u.role !== 'free' && (
                                <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-bold text-blue-700">
                                  {u.role}
                                </span>
                              )}
                            </div>
                            <div className="flex items-center gap-2 text-[12px] text-gray-500">
                              <span>{relTime(u.created_at)}</span>
                              {u.has_key && (
                                <span className="font-mono text-gray-400">{u.key_prefix}</span>
                              )}
                              {(() => {
                                const isCostSort =
                                  sortBy === 'cost_today' ||
                                  sortBy === 'cost_month' ||
                                  sortBy === 'cost_alltime';
                                const usageVal =
                                  sortBy === 'cost_month'
                                    ? Number(u.usage_month_usd ?? 0)
                                    : sortBy === 'cost_alltime'
                                      ? Number(u.usage_alltime_usd ?? 0)
                                      : Number(u.usage_today_usd ?? 0);
                                const suffix =
                                  sortBy === 'cost_month'
                                    ? '/mo'
                                    : sortBy === 'cost_alltime'
                                      ? '/all'
                                      : '/today';
                                // Cost sorts: always show usage (even without active key).
                                // Other sorts: only show if user has an active key.
                                if (isCostSort || u.has_key) {
                                  return (
                                    <span className="tabular-nums text-gray-700">
                                      ${usageVal.toFixed(2)} {suffix}
                                    </span>
                                  );
                                }
                                return null;
                              })()}
                            </div>
                          </div>

                          {/* Quick actions */}
                          <div
                            className="flex items-center gap-2 opacity-0 group-hover:opacity-100 transition"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {u.status === 'pending_approval' && (
                              <>
                                <button
                                  onClick={() => doApprove(u)}
                                  disabled={busy === u.id}
                                  className="rounded-md bg-gray-900 px-3 py-1 text-[12px] font-semibold text-white hover:bg-gray-800 transition disabled:opacity-50"
                                >
                                  Approve
                                </button>
                                <button
                                  onClick={() => {
                                    setRejectTarget(u);
                                    setRejectReason('');
                                  }}
                                  className="rounded-md px-3 py-1 text-[12px] font-semibold text-red-500 hover:bg-red-50 transition"
                                >
                                  Reject
                                </button>
                              </>
                            )}
                            {u.status === 'active' && u.has_key && (
                              <button
                                onClick={() => doRegen(u)}
                                disabled={busy === u.id}
                                className="rounded-md px-3 py-1 text-[12px] text-gray-400 hover:text-gray-900 hover:bg-gray-100 transition disabled:opacity-50"
                              >
                                Regenerate
                              </button>
                            )}
                          </div>

                          <svg
                            className={`h-4 w-4 shrink-0 text-gray-300 transition-transform ${
                              isOpen ? 'rotate-180' : ''
                            }`}
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                            strokeWidth={2}
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              d="M19.5 8.25l-7.5 7.5-7.5-7.5"
                            />
                          </svg>
                        </div>

                        {/* Rejected note */}
                        {u.status === 'rejected' && u.approval_note && !isOpen && (
                          <p className="pb-3 pl-16 text-[12px] text-red-400 italic">
                            &ldquo;{u.approval_note}&rdquo;
                          </p>
                        )}

                        {/* Detail */}
                        {isOpen && (
                          <div className="ml-12 mb-4 mt-1">
                            {detailLoading ? (
                              <div className="flex py-8">
                                <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                              </div>
                            ) : detail ? (
                              <div className="space-y-4 rounded-xl border border-gray-200 bg-gray-50 p-5">
                                {/* Stats */}
                                <div className="grid grid-cols-4 gap-3 text-[13px]">
                                  <div>
                                    <div className="text-[11px] font-medium text-gray-500">
                                      Today
                                    </div>
                                    <div className="mt-0.5 text-[16px] font-bold tabular-nums text-gray-900">
                                      ${Number(detail.usage_today_usd).toFixed(2)}
                                    </div>
                                    <div className="text-[11px] text-gray-400 tabular-nums">
                                      {detail.usage_today_requests} req
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] font-medium text-gray-500">
                                      This month
                                    </div>
                                    <div className="mt-0.5 text-[16px] font-bold tabular-nums text-gray-900">
                                      ${Number(detail.usage_month_usd).toFixed(2)}
                                    </div>
                                    <div className="text-[11px] text-gray-400 tabular-nums">
                                      {detail.usage_month_requests} req
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] font-medium text-gray-500">
                                      Last active
                                    </div>
                                    <div className="mt-0.5 text-[16px] font-bold text-gray-900">
                                      {relTime(detail.last_request_at)}
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] font-medium text-gray-500">
                                      Quota
                                    </div>
                                    <div className="mt-0.5 text-[16px] font-bold text-gray-900">
                                      {detail.quota_daily_usd
                                        ? `$${detail.quota_daily_usd}/d`
                                        : '-'}
                                    </div>
                                  </div>
                                </div>

                                {/* Models */}
                                {detail.models_used.length > 0 && (
                                  <div className="flex flex-wrap gap-1.5">
                                    {detail.models_used.map((m) => (
                                      <span
                                        key={m}
                                        className="rounded bg-white px-2 py-0.5 text-[11px] font-medium text-gray-600 border border-gray-200 shadow-sm"
                                      >
                                        {m.split('/').pop()}
                                      </span>
                                    ))}
                                  </div>
                                )}

                                {/* Edit (active users) */}
                                {u.status === 'active' && (
                                  <div className="flex items-end gap-3 border-t border-gray-200 pt-4">
                                    <div>
                                      <div className="text-[11px] font-medium text-gray-500 mb-1">
                                        Role
                                      </div>
                                      <select
                                        value={editRole}
                                        onChange={(e) => setEditRole(e.target.value)}
                                        className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                                      >
                                        <option value="free">free</option>
                                        <option value="pro">pro</option>
                                        <option value="internal">internal</option>
                                        <option value="admin">admin</option>
                                      </select>
                                    </div>
                                    {detail.has_key && (
                                      <>
                                        <div>
                                          <div className="text-[11px] font-medium text-gray-500 mb-1">
                                            Daily quota
                                          </div>
                                          <input
                                            type="number"
                                            value={editQuota}
                                            onChange={(e) => setEditQuota(e.target.value)}
                                            className="w-24 rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                                          />
                                        </div>
                                      </>
                                    )}
                                    <button
                                      onClick={doSave}
                                      disabled={saving}
                                      className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-gray-800 transition disabled:opacity-50"
                                    >
                                      {saving ? '...' : 'Save'}
                                    </button>
                                    <div className="flex-1" />
                                    <button
                                      onClick={() => {
                                        setDeleteTarget(u);
                                        setDeleteReason('');
                                      }}
                                      className="text-[12px] text-red-400 hover:text-red-600 transition"
                                    >
                                      Delete
                                    </button>
                                    <button
                                      onClick={() => doSuspend(u.id)}
                                      className="text-[12px] text-red-400 hover:text-red-600 transition"
                                    >
                                      Suspend
                                    </button>
                                  </div>
                                )}

                                {/* Suspended users */}
                                {u.status === 'suspended' && (
                                  <div className="flex items-center justify-between border-t border-gray-200 pt-4">
                                    <span className="text-[13px] text-gray-500">
                                      This user is suspended.
                                    </span>
                                    <div className="flex items-center gap-3">
                                      <button
                                        onClick={() => {
                                          setDeleteTarget(u);
                                          setDeleteReason('');
                                        }}
                                        className="text-[12px] text-red-400 hover:text-red-600 transition"
                                      >
                                        Delete
                                      </button>
                                      <button
                                        onClick={() => doReactivate(u.id)}
                                        className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-gray-800 transition"
                                      >
                                        Reactivate
                                      </button>
                                    </div>
                                  </div>
                                )}

                                {/* Deleted users */}
                                {u.status === 'deleted' && (
                                  <div className="flex items-center justify-between border-t border-gray-200 pt-4">
                                    <span className="text-[13px] text-gray-400">
                                      This user has been deleted.
                                    </span>
                                    <div className="flex items-center gap-3">
                                      <button
                                        onClick={() => {
                                          setHardDeleteTarget(u);
                                          setHardDeleteReason('');
                                          setHardDeleteEmailConfirm('');
                                        }}
                                        disabled={busy === u.id}
                                        className="text-[12px] text-red-500 hover:text-red-700 transition disabled:opacity-50 disabled:cursor-not-allowed"
                                      >
                                        Permanently Delete
                                      </button>
                                      <button
                                        onClick={() => doResume(u)}
                                        disabled={busy === u.id}
                                        className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-gray-800 transition disabled:opacity-50"
                                      >
                                        Resume
                                      </button>
                                    </div>
                                  </div>
                                )}
                              </div>
                            ) : null}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </>
        )}

        {/* ========== Audit Log Tab ========== */}
        {activeTab === 'audit' && (
          <div className="mt-6">
            {/* Action filter */}
            <div className="flex flex-wrap items-center gap-3">
              <select
                value={auditFilter}
                onChange={(e) => {
                  setAuditFilter(e.target.value);
                  setAuditOffset(0);
                }}
                className="rounded-md border border-gray-200 bg-white px-3 py-1.5 text-[13px]"
              >
                <option value="">All actions</option>
                {AUDIT_ACTIONS.map((a) => (
                  <option key={a} value={a}>
                    {actionLabel(a)}
                  </option>
                ))}
              </select>
              <input
                type="text"
                value={auditUserFilter}
                onChange={(e) => {
                  setAuditUserFilter(e.target.value);
                  setAuditOffset(0);
                }}
                placeholder="Filter by user ID…"
                className="min-w-[180px] rounded-lg border border-gray-200 bg-white px-4 py-2 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
              />
              <span className="text-[12px] text-gray-400 tabular-nums">{auditTotal} entries</span>
            </div>

            {/* Entries */}
            <div className="mt-4">
              {auditLoading ? (
                <div className="flex justify-center py-24">
                  <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                </div>
              ) : auditEntries.length === 0 ? (
                <div className="py-24 text-center">
                  <p className="text-[13px] text-gray-400">No audit log entries.</p>
                </div>
              ) : (
                <div>
                  {auditEntries.map((entry, i) => {
                    const cat = actionCategory(entry.action);
                    const badgeClass = entry.success
                      ? AUDIT_CATEGORY_CLASS[cat]
                      : 'bg-red-50 text-red-700';
                    const rowClass = [
                      'py-3',
                      i > 0 ? 'border-t border-gray-100' : '',
                      !entry.success ? 'border-l-2 border-rose-300 pl-3' : '',
                    ]
                      .filter(Boolean)
                      .join(' ');
                    const absoluteTs = new Date(entry.timestamp).toLocaleString();
                    const tid = entry.target_user_id;
                    const tidDisplay = tid && tid.length > 12 ? `${tid.slice(0, 8)}…` : tid;
                    const detailEntries =
                      entry.details && typeof entry.details === 'object'
                        ? Object.entries(entry.details)
                        : [];
                    return (
                      <div
                        key={entry.id}
                        className={rowClass}
                        style={
                          entry.success ? { paddingLeft: 4, paddingRight: 4 } : { paddingRight: 4 }
                        }
                      >
                        <div className="flex items-center gap-3">
                          {!entry.success && (
                            <span className="inline-block rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-red-700">
                              FAILED
                            </span>
                          )}
                          <span
                            className={`inline-block rounded px-2 py-0.5 text-[11px] font-bold ${badgeClass}`}
                          >
                            {actionLabel(entry.action)}
                          </span>
                          {tid && (
                            <button
                              onClick={() => {
                                setAuditUserFilter(tid);
                                setAuditOffset(0);
                              }}
                              title={tid}
                              aria-label={`Filter by user ${tid}`}
                              className="font-mono text-[12px] text-gray-500 hover:text-gray-900 hover:underline"
                            >
                              <span aria-hidden="true">{tidDisplay}</span>
                              <span className="sr-only">{tid}</span>
                            </button>
                          )}
                          <time
                            dateTime={entry.timestamp}
                            title={absoluteTs}
                            aria-label={absoluteTs}
                            className="ml-auto text-[12px] text-gray-400"
                          >
                            {formatRelative(entry.timestamp)}
                          </time>
                        </div>
                        {detailEntries.length > 0 && (
                          <>
                            <div className="flex flex-wrap gap-1.5 mt-1.5">
                              {detailEntries.map(([k, v]) => {
                                const { display, full } = formatAuditDetailValue(v);
                                return (
                                  <span
                                    key={k}
                                    title={full}
                                    className="inline-flex items-center gap-1 rounded bg-gray-50 border border-gray-100 px-1.5 py-0.5 text-[11px] text-gray-700"
                                  >
                                    <span className="text-gray-400">{k}:</span>
                                    <span>{display}</span>
                                  </span>
                                );
                              })}
                            </div>
                            <RawJsonDetails data={entry.details} />
                          </>
                        )}
                        <div className="mt-1 text-[11px] text-gray-400">from {entry.admin_ip}</div>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Pagination */}
              {auditTotal > AUDIT_PAGE_SIZE && (
                <div className="mt-4 flex items-center justify-between">
                  <span className="text-[12px] text-gray-400 tabular-nums">
                    {auditOffset + 1}&ndash;{Math.min(auditOffset + AUDIT_PAGE_SIZE, auditTotal)} of{' '}
                    {auditTotal}
                  </span>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => setAuditOffset(Math.max(0, auditOffset - AUDIT_PAGE_SIZE))}
                      disabled={auditOffset === 0}
                      className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
                    >
                      Prev
                    </button>
                    <button
                      onClick={() => setAuditOffset(auditOffset + AUDIT_PAGE_SIZE)}
                      disabled={auditOffset + AUDIT_PAGE_SIZE >= auditTotal}
                      className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* ========== Providers Tab ========== */}
        {activeTab === 'providers' && (
          <div className="mt-6">
            {providerQuotasLoading ? (
              <div className="flex justify-center py-24">
                <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
              </div>
            ) : providerQuotas.length === 0 ? (
              <div className="py-24 text-center">
                <p className="text-[13px] text-gray-400">No provider data.</p>
              </div>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                {providerQuotas.map((p) => (
                  <ProviderCard key={p.name} provider={p} />
                ))}
              </div>
            )}
          </div>
        )}

        {/* ========== Requests Tab ========== */}
        {activeTab === 'requests' && (
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
                {reqMetricsLoading && (
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                )}
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
            <div className="flex flex-wrap items-center gap-3">
              <input
                type="text"
                value={reqUserFilter}
                onChange={(e) => {
                  setReqUserFilter(e.target.value);
                  setReqOffset(0);
                }}
                placeholder="Filter by user ID..."
                className="flex-1 min-w-[160px] rounded-lg border border-gray-200 bg-white px-4 py-2 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
              />
              <input
                type="text"
                value={reqModelFilter}
                onChange={(e) => {
                  setReqModelFilter(e.target.value);
                  setReqOffset(0);
                }}
                placeholder="Filter by model..."
                className="flex-1 min-w-[160px] rounded-lg border border-gray-200 bg-white px-4 py-2 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
              />
              <label className="flex items-center gap-1.5 text-[13px] text-gray-600 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={reqErrorsOnly}
                  onChange={(e) => {
                    setReqErrorsOnly(e.target.checked);
                    setReqOffset(0);
                  }}
                  className="rounded border-gray-300"
                />
                Errors only
              </label>
              <span className="text-[12px] text-gray-400 tabular-nums">{reqTotal} entries</span>
              <button
                type="button"
                onClick={() => setShowExportPanel((v) => !v)}
                className="ml-auto rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50"
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
                          setToast(
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
            <div className="mt-4">
              {reqLoading ? (
                <div className="flex justify-center py-24">
                  <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                </div>
              ) : reqEntries.length === 0 ? (
                <div className="py-24 text-center">
                  <p className="text-[13px] text-gray-400">No requests found.</p>
                </div>
              ) : (
                <div className="-mx-1">
                  <table className="min-w-full">
                    <thead className="bg-gray-50">
                      <tr className="border-b border-gray-200">
                        <th className="py-2 pl-4 pr-3 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Model
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          User
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          IP
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Status
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Latency
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Tokens
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
                          req.status_code != null &&
                          req.status_code >= 200 &&
                          req.status_code < 400;
                        const isExpanded = reqExpandedId === req.request_id;
                        const hasCacheTokens =
                          req.cache_read_tokens != null || req.cache_write_tokens != null;
                        const cachedTokens = hasCacheTokens
                          ? (req.cache_read_tokens ?? 0) + (req.cache_write_tokens ?? 0)
                          : null;
                        return (
                          <Fragment key={req.request_id}>
                            <tr
                              className="border-b border-gray-100 hover:bg-gray-50/60 cursor-pointer transition-colors"
                              onClick={() => setReqExpandedId(isExpanded ? null : req.request_id)}
                            >
                              <td className="whitespace-nowrap py-2.5 pl-4 pr-3 text-[13px]">
                                <div className="font-medium text-gray-900">{req.model_id}</div>
                                <div className="text-[11px] text-gray-400">{req.provider}</div>
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
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px] font-mono text-gray-500">
                                {req.user_ip ?? <span className="text-gray-300">—</span>}
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
                                <td colSpan={8} className="px-4 py-3">
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
                                    <div>
                                      <span className="text-gray-500">Reasoning:</span>{' '}
                                      <span className="text-gray-700">
                                        {req.reasoning_tokens != null
                                          ? req.reasoning_tokens.toLocaleString()
                                          : '—'}
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
                                    <FoldedText label="Prompt" value={req.prompt} />
                                    <FoldedText label="Response" value={req.response} />
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
                <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <span className="text-[12px] text-gray-400 tabular-nums text-center sm:text-left">
                    {reqOffset + 1}&ndash;{Math.min(reqOffset + REQ_PAGE_SIZE, reqTotal)} of{' '}
                    {reqTotal}
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
                            applyOffsetJump(
                              reqJumpPage,
                              reqTotal,
                              REQ_PAGE_SIZE,
                              setReqOffset,
                              () => setReqJumpPage(''),
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
        )}
        {activeTab === 'analytics' && <AnalyticsTab />}
        {activeTab === 'provider-perf' && <ProviderPerformanceTab />}
        {activeTab === 'token-usage' && <TokenUsageTab />}

        {activeTab === 'performance' && (
          <div className="mt-5">
            <div className="mb-2 flex items-center justify-between">
              <div>
                <h2 className="text-[14px] font-semibold text-gray-900">Performance metrics</h2>
                <p className="text-[11px] text-gray-400">
                  Prompt/response length, time-to-first-token, and inter-token latency
                  distributions.
                </p>
              </div>
              {perfMetricsLoading && (
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
              )}
            </div>
            {perfMetrics.length > 0 ? (
              <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-2">
                {perfMetrics.map((metric) => (
                  <PerformanceMetricsCard key={metric.key} metric={metric} />
                ))}
              </div>
            ) : !perfMetricsLoading ? (
              <div className="rounded-xl border border-dashed border-gray-200 py-8 text-center">
                <p className="text-[13px] text-gray-400">No performance metrics available.</p>
              </div>
            ) : null}
          </div>
        )}

        {/* ========== Broadcast Email Tab ========== */}
        {activeTab === 'broadcast' && (
          <>
            <div className="mt-8 space-y-6">
              {/* Composer */}
              <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
                <h2 className="text-[15px] font-semibold text-gray-900 mb-4">Compose Broadcast</h2>

                {/* Template selector */}
                <div className="mb-4">
                  <label className="block text-[12px] font-medium text-gray-600 mb-1">
                    Template
                  </label>
                  <select
                    value={bcTemplateKey}
                    onChange={(e) => {
                      setBcTemplateKey(e.target.value);
                      setBcTemplateVars({});
                      setBcSubject('');
                      setBcBodyHtml('');
                      setBcPreview(null);
                    }}
                    className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                  >
                    <option value="custom">Custom</option>
                    <option value="maintenance">Maintenance Notice</option>
                    <option value="announcement">Announcement</option>
                    <option value="quota_change">Quota Change</option>
                  </select>
                </div>

                {/* Template variable fields */}
                {bcTemplateKey === 'maintenance' && (
                  <div className="mb-4 grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">
                        Date
                      </label>
                      <input
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="e.g. May 1, 2026"
                        value={bcTemplateVars['date'] ?? ''}
                        onChange={(e) => setBcTemplateVars((v) => ({ ...v, date: e.target.value }))}
                      />
                    </div>
                    <div>
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">
                        Duration
                      </label>
                      <input
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="e.g. 2 hours"
                        value={bcTemplateVars['duration'] ?? ''}
                        onChange={(e) =>
                          setBcTemplateVars((v) => ({ ...v, duration: e.target.value }))
                        }
                      />
                    </div>
                  </div>
                )}
                {bcTemplateKey === 'announcement' && (
                  <div className="mb-4 space-y-3">
                    <div>
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">
                        Feature Name
                      </label>
                      <input
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="e.g. GPT-5 Support"
                        value={bcTemplateVars['feature_name'] ?? ''}
                        onChange={(e) =>
                          setBcTemplateVars((v) => ({ ...v, feature_name: e.target.value }))
                        }
                      />
                    </div>
                    <div>
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">
                        Description
                      </label>
                      <textarea
                        rows={3}
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="Describe the new feature..."
                        value={bcTemplateVars['description'] ?? ''}
                        onChange={(e) =>
                          setBcTemplateVars((v) => ({ ...v, description: e.target.value }))
                        }
                      />
                    </div>
                  </div>
                )}
                {bcTemplateKey === 'quota_change' && (
                  <div className="mb-4">
                    <label className="block text-[12px] font-medium text-gray-600 mb-1">
                      New Quota
                    </label>
                    <input
                      className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                      placeholder="e.g. $50/day"
                      value={bcTemplateVars['new_quota'] ?? ''}
                      onChange={(e) =>
                        setBcTemplateVars((v) => ({ ...v, new_quota: e.target.value }))
                      }
                    />
                  </div>
                )}
                {bcTemplateKey === 'custom' && (
                  <div className="mb-4 space-y-3">
                    <div>
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">
                        Subject
                      </label>
                      <input
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="Email subject"
                        value={bcSubject}
                        onChange={(e) => setBcSubject(e.target.value)}
                      />
                    </div>
                    <div>
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">
                        Body (HTML)
                      </label>
                      <textarea
                        rows={6}
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] font-mono focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="<p>Your message here...</p>"
                        value={bcBodyHtml}
                        onChange={(e) => setBcBodyHtml(e.target.value)}
                      />
                    </div>
                  </div>
                )}

                {/* Recipient filters */}
                <div className="mb-4 grid grid-cols-2 gap-6">
                  <div>
                    <label className="block text-[12px] font-medium text-gray-600 mb-2">
                      Roles
                    </label>
                    {['free', 'internal', 'admin'].map((role) => (
                      <label
                        key={role}
                        className="flex items-center gap-2 text-[13px] text-gray-700 mb-1"
                      >
                        <input
                          type="checkbox"
                          checked={bcTargetRoles.includes(role)}
                          onChange={(e) =>
                            setBcTargetRoles((prev) =>
                              e.target.checked ? [...prev, role] : prev.filter((r) => r !== role),
                            )
                          }
                        />
                        {role}
                      </label>
                    ))}
                  </div>
                  <div>
                    <label className="block text-[12px] font-medium text-gray-600 mb-2">
                      Statuses
                    </label>
                    {['active', 'suspended', 'pending_approval', 'rejected'].map((status) => (
                      <label
                        key={status}
                        className="flex items-center gap-2 text-[13px] text-gray-700 mb-1"
                      >
                        <input
                          type="checkbox"
                          checked={bcTargetStatuses.includes(status)}
                          onChange={(e) =>
                            setBcTargetStatuses((prev) =>
                              e.target.checked
                                ? [...prev, status]
                                : prev.filter((s) => s !== status),
                            )
                          }
                        />
                        {status.replace('_', ' ')}
                      </label>
                    ))}
                  </div>
                </div>

                {/* Schedule toggle */}
                <div className="mb-4">
                  <label className="block text-[12px] font-medium text-gray-600 mb-2">
                    Send Timing
                  </label>
                  <div className="flex items-center gap-4">
                    <label className="flex items-center gap-2 text-[13px] text-gray-700">
                      <input
                        type="radio"
                        checked={bcScheduleMode === 'now'}
                        onChange={() => setBcScheduleMode('now')}
                      />
                      Send now
                    </label>
                    <label className="flex items-center gap-2 text-[13px] text-gray-700">
                      <input
                        type="radio"
                        checked={bcScheduleMode === 'later'}
                        onChange={() => setBcScheduleMode('later')}
                      />
                      Schedule for later
                    </label>
                  </div>
                  {bcScheduleMode === 'later' && (
                    <input
                      type="datetime-local"
                      className="mt-2 rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                      value={bcScheduledAt}
                      onChange={(e) => setBcScheduledAt(e.target.value)}
                    />
                  )}
                </div>

                {/* Action buttons */}
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    disabled={bcPreviewLoading}
                    onClick={async () => {
                      setBcPreviewLoading(true);
                      try {
                        const res = await previewBroadcast({
                          template_key: bcTemplateKey === 'custom' ? null : bcTemplateKey,
                          template_vars: bcTemplateVars,
                          subject: bcSubject,
                          body_html: bcBodyHtml,
                          body_text: '',
                          target_roles: bcTargetRoles,
                          target_statuses: bcTargetStatuses,
                        });
                        setBcPreview(res);
                      } catch (err) {
                        setToast(getErrorMessage(err));
                      } finally {
                        setBcPreviewLoading(false);
                      }
                    }}
                    className="rounded-md border border-gray-300 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
                  >
                    {bcPreviewLoading ? 'Loading…' : 'Preview & Count'}
                  </button>

                  <button
                    disabled={bcTestLoading}
                    onClick={async () => {
                      setBcTestLoading(true);
                      try {
                        await sendTestBroadcastEmail({
                          template_key: bcTemplateKey === 'custom' ? null : bcTemplateKey,
                          template_vars: bcTemplateVars,
                          subject: bcSubject,
                          body_html: bcBodyHtml,
                          body_text: '',
                          target_roles: bcTargetRoles,
                          target_statuses: bcTargetStatuses,
                        });
                        setToast('Test email sent to your address');
                      } catch (err) {
                        setToast(getErrorMessage(err));
                      } finally {
                        setBcTestLoading(false);
                      }
                    }}
                    className="rounded-md border border-gray-300 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
                  >
                    {bcTestLoading ? 'Sending…' : 'Send Test to Me'}
                  </button>

                  <button
                    onClick={() => setBcConfirm(true)}
                    disabled={bcSending}
                    className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
                  >
                    {bcScheduleMode === 'later' ? 'Schedule' : 'Send Now'}
                  </button>
                </div>

                {/* Preview panel */}
                {bcPreview && (
                  <div className="mt-4 rounded-lg border border-blue-100 bg-blue-50 p-4">
                    <div className="text-[13px] font-medium text-blue-800 mb-1">
                      {bcPreview.recipient_count} recipient
                      {bcPreview.recipient_count !== 1 ? 's' : ''} match your filters
                    </div>
                    <div className="text-[12px] text-blue-700">
                      Subject: {bcPreview.rendered_subject}
                    </div>
                  </div>
                )}
              </div>

              {/* Confirmation modal */}
              {bcConfirm && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
                  <div className="w-full max-w-sm rounded-xl bg-white p-6 shadow-xl">
                    <h3 className="text-[15px] font-semibold text-gray-900 mb-2">
                      Confirm Broadcast
                    </h3>
                    <p className="text-[13px] text-gray-600 mb-1">
                      {bcPreview
                        ? `This will send to ${bcPreview.recipient_count} recipient(s).`
                        : 'Send broadcast email?'}
                    </p>
                    {bcScheduleMode === 'later' && bcScheduledAt && (
                      <p className="text-[12px] text-gray-500 mb-4">
                        Scheduled for: {new Date(bcScheduledAt).toLocaleString()}
                      </p>
                    )}
                    <div className="flex justify-end gap-3 mt-4">
                      <button
                        onClick={() => setBcConfirm(false)}
                        className="rounded-md border border-gray-200 px-4 py-2 text-[13px] text-gray-700 hover:bg-gray-50"
                      >
                        Cancel
                      </button>
                      <button
                        disabled={bcSending}
                        onClick={async () => {
                          setBcSending(true);
                          setBcConfirm(false);
                          try {
                            await createBroadcast({
                              template_key: bcTemplateKey === 'custom' ? null : bcTemplateKey,
                              template_vars: bcTemplateVars,
                              subject: bcSubject,
                              body_html: bcBodyHtml,
                              body_text: '',
                              target_roles: bcTargetRoles,
                              target_statuses: bcTargetStatuses,
                              scheduled_at:
                                bcScheduleMode === 'later' && bcScheduledAt
                                  ? new Date(bcScheduledAt).toISOString()
                                  : null,
                            });
                            setToast(
                              bcScheduleMode === 'later'
                                ? 'Broadcast scheduled'
                                : 'Broadcast queued',
                            );
                            await loadBroadcasts();
                          } catch (err) {
                            setToast(getErrorMessage(err));
                          } finally {
                            setBcSending(false);
                          }
                        }}
                        className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
                      >
                        Confirm
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {/* History table */}
              <div className="rounded-xl border border-gray-200 bg-white shadow-sm">
                <div className="px-6 py-4 border-b border-gray-100">
                  <h2 className="text-[15px] font-semibold text-gray-900">Send History</h2>
                </div>
                {broadcastLoading ? (
                  <div className="px-6 py-8 text-[13px] text-gray-400">Loading…</div>
                ) : broadcasts.length === 0 ? (
                  <div className="px-6 py-8 text-[13px] text-gray-400">No broadcasts yet.</div>
                ) : (
                  <table className="w-full text-[13px]">
                    <thead>
                      <tr className="border-b border-gray-100 text-left text-[11px] font-medium text-gray-500">
                        <th className="px-6 py-3">Subject</th>
                        <th className="px-6 py-3">Status</th>
                        <th className="px-6 py-3">Recipients</th>
                        <th className="px-6 py-3">Sent / Scheduled</th>
                        <th className="px-6 py-3">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {broadcasts.map((bc) => (
                        <Fragment key={bc.id}>
                          <tr
                            className="border-b border-gray-50 hover:bg-gray-50 cursor-pointer"
                            onClick={async () => {
                              if (broadcastDetail?.broadcast.id === bc.id) {
                                setBroadcastDetail(null);
                                return;
                              }
                              setBroadcastDetailLoading(true);
                              try {
                                const detail = await getBroadcastDetail(bc.id);
                                setBroadcastDetail(detail);
                              } catch {
                                /* ignore */
                              } finally {
                                setBroadcastDetailLoading(false);
                              }
                            }}
                          >
                            <td className="px-6 py-3 max-w-[200px] truncate">{bc.subject}</td>
                            <td className="px-6 py-3">
                              <span
                                className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${
                                  bc.status === 'sent'
                                    ? 'bg-green-50 text-green-700'
                                    : bc.status === 'failed'
                                      ? 'bg-red-50 text-red-700'
                                      : bc.status === 'sending'
                                        ? 'bg-blue-50 text-blue-700'
                                        : bc.status === 'cancelled'
                                          ? 'bg-gray-100 text-gray-500'
                                          : 'bg-yellow-50 text-yellow-700'
                                }`}
                              >
                                {bc.status}
                              </span>
                            </td>
                            <td className="px-6 py-3">{bc.recipient_count.toLocaleString()}</td>
                            <td className="px-6 py-3 text-gray-500">
                              {bc.sent_at
                                ? relTime(bc.sent_at)
                                : bc.scheduled_at
                                  ? new Date(bc.scheduled_at).toLocaleString()
                                  : '—'}
                            </td>
                            <td className="px-6 py-3">
                              {bc.status === 'scheduled' && (
                                <button
                                  onClick={async (e) => {
                                    e.stopPropagation();
                                    if (!confirm('Cancel this scheduled broadcast?')) return;
                                    try {
                                      await cancelBroadcast(bc.id);
                                      setToast('Broadcast cancelled');
                                      await loadBroadcasts();
                                    } catch (err) {
                                      setToast(getErrorMessage(err));
                                    }
                                  }}
                                  className="text-red-500 hover:underline text-[12px]"
                                >
                                  Cancel
                                </button>
                              )}
                            </td>
                          </tr>
                          {/* Detail drawer */}
                          {broadcastDetail?.broadcast.id === bc.id && (
                            <tr>
                              <td colSpan={5} className="bg-gray-50 px-6 py-4">
                                {broadcastDetailLoading ? (
                                  <span className="text-[12px] text-gray-400">
                                    Loading recipients…
                                  </span>
                                ) : (
                                  <>
                                    <div className="text-[12px] font-medium text-gray-600 mb-2">
                                      Recipients ({broadcastDetail.total_recipients})
                                    </div>
                                    <div className="overflow-x-auto">
                                      <table className="w-full text-[12px]">
                                        <thead>
                                          <tr className="text-left text-[10px] font-medium text-gray-400">
                                            <th className="pr-4 py-1">Email</th>
                                            <th className="pr-4 py-1">Status</th>
                                            <th className="pr-4 py-1">Error</th>
                                            <th className="pr-4 py-1">Sent At</th>
                                          </tr>
                                        </thead>
                                        <tbody>
                                          {broadcastDetail.recipients.map((r) => (
                                            <tr
                                              key={r.user_id}
                                              className="border-t border-gray-100"
                                            >
                                              <td className="pr-4 py-1 text-gray-700">{r.email}</td>
                                              <td className="pr-4 py-1">
                                                <span
                                                  className={
                                                    r.status === 'sent'
                                                      ? 'text-green-600'
                                                      : r.status === 'failed'
                                                        ? 'text-red-500'
                                                        : 'text-gray-400'
                                                  }
                                                >
                                                  {r.status}
                                                </span>
                                              </td>
                                              <td className="pr-4 py-1 text-red-400">
                                                {r.error ?? '—'}
                                              </td>
                                              <td className="pr-4 py-1 text-gray-400">
                                                {r.sent_at ? relTime(r.sent_at) : '—'}
                                              </td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </div>
                                  </>
                                )}
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </>
        )}
      </div>

      {/* Reject modal */}
      {rejectTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/20 backdrop-blur-[2px]"
            onClick={() => {
              setRejectTarget(null);
              setRejectReason('');
            }}
          />
          <div className="relative mx-4 w-full max-w-sm rounded-xl border border-gray-200 bg-white p-5 shadow-2xl">
            <h3 className="text-[15px] font-semibold text-gray-900">Reject {rejectTarget.email}</h3>
            <textarea
              className="mt-3 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] placeholder:text-gray-300 focus:border-gray-400 focus:outline-none"
              rows={3}
              placeholder="Reason (sent to user)..."
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              autoFocus
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => {
                  setRejectTarget(null);
                  setRejectReason('');
                }}
                className="rounded-md px-3 py-1.5 text-[13px] text-gray-400 hover:text-gray-900 transition"
              >
                Cancel
              </button>
              <button
                onClick={doReject}
                disabled={!rejectReason.trim() || busy === rejectTarget.id}
                className="rounded-md bg-red-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-red-700 transition disabled:opacity-50"
              >
                {busy === rejectTarget.id ? '...' : 'Reject'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete modal */}
      {deleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/20 backdrop-blur-[2px]"
            onClick={() => {
              setDeleteTarget(null);
              setDeleteReason('');
            }}
          />
          <div className="relative mx-4 w-full max-w-sm rounded-xl border border-gray-200 bg-white p-5 shadow-2xl">
            <h3 className="text-[15px] font-semibold text-gray-900">Delete {deleteTarget.email}</h3>
            <p className="mt-1 text-[12px] text-gray-400">
              This will revoke API keys, purge sessions, and set the account to deleted.
            </p>
            <textarea
              className="mt-3 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] placeholder:text-gray-300 focus:border-gray-400 focus:outline-none"
              rows={3}
              placeholder="Reason for deletion..."
              value={deleteReason}
              onChange={(e) => setDeleteReason(e.target.value)}
              autoFocus
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => {
                  setDeleteTarget(null);
                  setDeleteReason('');
                }}
                className="rounded-md px-3 py-1.5 text-[13px] text-gray-400 hover:text-gray-900 transition"
              >
                Cancel
              </button>
              <button
                onClick={doDelete}
                disabled={!deleteReason.trim() || busy === deleteTarget.id}
                className="rounded-md bg-red-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-red-700 transition disabled:opacity-50"
              >
                {busy === deleteTarget.id ? '...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Hard-delete (permanent) modal */}
      {hardDeleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/30 backdrop-blur-[2px]"
            onClick={() => {
              setHardDeleteTarget(null);
              setHardDeleteReason('');
              setHardDeleteEmailConfirm('');
            }}
          />
          <div className="relative mx-4 w-full max-w-md rounded-xl border border-red-200 bg-white p-5 shadow-2xl">
            <h3 className="text-[15px] font-semibold text-red-700">
              Permanently delete {hardDeleteTarget.email}
            </h3>
            <p className="mt-2 text-[12px] text-gray-600">
              This will <span className="font-semibold text-red-700">permanently wipe</span> the
              user row, all API keys, all api_logs, and prior audit-log entries for this user. This
              action <span className="font-semibold">cannot be undone</span>.
            </p>
            <p className="mt-3 text-[12px] text-gray-500">
              Type the user&apos;s email address (
              <span className="font-mono text-gray-700">{hardDeleteTarget.email}</span>) to confirm:
            </p>
            <input
              type="text"
              className="mt-2 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] font-mono placeholder:text-gray-300 focus:border-red-400 focus:outline-none"
              placeholder="email@example.com"
              value={hardDeleteEmailConfirm}
              onChange={(e) => setHardDeleteEmailConfirm(e.target.value)}
              autoFocus
            />
            <textarea
              className="mt-3 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] placeholder:text-gray-300 focus:border-gray-400 focus:outline-none"
              rows={2}
              placeholder="Reason (optional, audit trail)..."
              value={hardDeleteReason}
              onChange={(e) => setHardDeleteReason(e.target.value)}
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => {
                  setHardDeleteTarget(null);
                  setHardDeleteReason('');
                  setHardDeleteEmailConfirm('');
                }}
                className="rounded-md px-3 py-1.5 text-[13px] text-gray-500 hover:text-gray-900 transition"
              >
                Cancel
              </button>
              <button
                onClick={doHardDelete}
                disabled={
                  hardDeleteEmailConfirm !== hardDeleteTarget.email || busy === hardDeleteTarget.id
                }
                className="rounded-md bg-red-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-red-700 transition disabled:opacity-50"
              >
                {busy === hardDeleteTarget.id ? '...' : 'Permanently Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </ProtectedRoute>
  );
}
