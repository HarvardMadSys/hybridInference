'use client';

import { Fragment, useCallback, useEffect, useId, useRef, useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { useAuth } from '@/components/providers';
import { InlineErrorText } from '@/components/ui/InlineErrorText';
import {
  AdminRecentRequestItem,
  AdminRequestMetricsWindow,
  AuditLogEntry,
  BroadcastDetailResponse,
  BroadcastListItem,
  ProviderQuotaResult,
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
  getProviderQuotas,
  getRecentRequestContent,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AnalyticsTab } from './AnalyticsTab';
import { ProviderPerformanceTab } from './ProviderPerformanceTab';
import { SettingsTab } from './SettingsTab';
import { TokenUsageTab } from './TokenUsageTab';
import UsersTab from './users';

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

function compactRequestSurface(surface?: string | null): string {
  if (surface === 'anthropic_messages') return 'Anthropic';
  if (surface === 'openai_chat_completions') return 'OpenAI';
  return surface || 'API';
}

function compactUserAgent(userAgent?: string | null): string {
  if (!userAgent) return '—';
  const lower = userAgent.toLowerCase();
  if (lower.includes('cursor')) return 'Cursor';
  if (lower.includes('claude-code')) return 'Claude Code';
  if (lower.includes('anthropic')) return 'Anthropic SDK';
  if (lower.includes('openai')) return 'OpenAI SDK';
  if (lower.includes('python')) return 'Python';
  if (lower.includes('node') || lower.includes('undici')) return 'Node';
  if (lower.includes('curl')) return 'curl';
  if (lower.includes('mozilla')) return 'Browser';
  return userAgent.split(/[ /]/, 1)[0] || userAgent;
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

function isChatMessageArray(v: unknown): v is ChatMessage[] {
  return Array.isArray(v) && v.every(isChatMessage);
}

type AnthropicMessageResponse = Record<string, unknown> & {
  type: 'message';
  role: string;
  content: unknown;
};

function isAnthropicMessageResponse(v: unknown): v is AnthropicMessageResponse {
  if (!isRecord(v)) return false;
  if (v.type !== 'message') return false;
  if (typeof v.role !== 'string') return false;
  if (!('content' in v)) return false;
  const c = v.content;
  return typeof c === 'string' || Array.isArray(c);
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
  if (isChatMessageArray(data)) {
    return (
      <div className="mt-1 rounded-md border border-gray-200 bg-white px-3 py-2">
        <div className="space-y-1.5">
          {data.map((m, i) => (
            <MessageBlock key={`${i}-${m.role}`} message={m} />
          ))}
        </div>
      </div>
    );
  }
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
  if (isAnthropicMessageResponse(data)) {
    const message: ChatMessage = { role: data.role, content: data.content };
    return (
      <div className="mt-1 rounded-md border border-gray-200 bg-white px-3 py-2">
        <MetaList data={data} skip={['content', 'role', 'type']} />
        <div className="space-y-1.5">
          <MessageBlock message={message} />
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

function previewFromMessages(messages: ChatMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === 'user') {
      const text = flattenContent(m.content);
      if (text) return previewText(text);
    }
  }
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
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
  const last = messages[messages.length - 1];
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
  return null;
}

function computePreview(parsed: unknown, fallback: string): string {
  if (isChatMessageArray(parsed)) {
    const p = previewFromMessages(parsed);
    if (p !== null) return p;
  }
  if (hasMessages(parsed)) {
    const p = previewFromMessages(parsed.messages);
    if (p !== null) return p;
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
  if (isAnthropicMessageResponse(parsed)) {
    const p = previewFromMessages([{ role: parsed.role, content: parsed.content }]);
    if (p !== null) return p;
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
  const isAdmin = state.user?.is_admin === true;

  // Top-level tab
  const [activeTab, setActiveTab] = useState<
    'users' | 'audit' | 'requests' | 'broadcast' | 'providers' | 'analytics' | 'usage' | 'settings'
  >('users');

  // Providers sub-tab
  const [providerSubTab, setProviderSubTab] = useState<'quota' | 'performance'>('quota');

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get('tab');
    const sub = params.get('sub');
    if (tab === 'performance' || tab === 'provider-perf') {
      setActiveTab('providers');
      setProviderSubTab('performance');
      params.set('tab', 'providers');
      params.set('sub', 'performance');
      const next = `${window.location.pathname}?${params.toString()}`;
      window.history.replaceState({}, '', next);
    } else if (
      tab === 'users' ||
      tab === 'audit' ||
      tab === 'requests' ||
      tab === 'broadcast' ||
      tab === 'providers' ||
      tab === 'analytics' ||
      tab === 'usage' ||
      tab === 'settings'
    ) {
      setActiveTab(
        tab as
          | 'users'
          | 'audit'
          | 'requests'
          | 'broadcast'
          | 'providers'
          | 'analytics'
          | 'usage'
          | 'settings',
      );
      if (tab === 'providers' && sub === 'performance') {
        setProviderSubTab('performance');
      }
    }
  }, []);

  // Shared error/toast state surfaced by tab children
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  // Users tab loading state surfaced for the global Refresh button
  const [usersLoading, setUsersLoading] = useState(false);
  const [usersRefreshNonce, setUsersRefreshNonce] = useState(0);

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
  const [perfRefreshNonce, setPerfRefreshNonce] = useState(0);
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
    if (!isAdmin) return;
    if (activeTab === 'audit') loadAudit();
  }, [loadAudit, activeTab, isAdmin]);

  useEffect(() => {
    if (!isAdmin) return;
    if (activeTab === 'requests') {
      loadRequests();
      loadRequestMetrics();
    }
  }, [loadRequests, loadRequestMetrics, activeTab, isAdmin]);

  useEffect(() => {
    if (!isAdmin) return;
    if (activeTab === 'providers' && providerSubTab === 'quota') loadProviderQuotas();
  }, [loadProviderQuotas, activeTab, providerSubTab, isAdmin]);

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
    if (!isAdmin) return;
    if (activeTab === 'broadcast') loadBroadcasts();
  }, [loadBroadcasts, activeTab, isAdmin]);

  if (!isAdmin) {
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

  const onTabChange = (
    tab:
      | 'users'
      | 'audit'
      | 'requests'
      | 'broadcast'
      | 'providers'
      | 'analytics'
      | 'usage'
      | 'settings',
  ) => {
    setActiveTab(tab);
    const params = new URLSearchParams(window.location.search);
    params.set('tab', tab);
    if (tab === 'providers') {
      params.set('sub', providerSubTab);
    } else {
      params.delete('sub');
    }
    const next = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState({}, '', next);
  };

  const refreshActiveTab = () => {
    if (activeTab === 'users') {
      setUsersRefreshNonce((n) => n + 1);
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
      if (providerSubTab === 'quota') {
        loadProviderQuotas();
      } else {
        setPerfRefreshNonce((n) => n + 1);
      }
      return;
    }
    if (activeTab === 'analytics') {
      return;
    }
    if (activeTab === 'usage') {
      setPerfRefreshNonce((n) => n + 1);
      return;
    }
    if (activeTab === 'settings') {
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
              usersLoading ||
              auditLoading ||
              reqLoading ||
              reqMetricsLoading ||
              providerQuotasLoading
            }
            className="text-[13px] text-gray-400 transition hover:text-gray-900 disabled:opacity-40"
          >
            {usersLoading ||
            auditLoading ||
            reqLoading ||
            reqMetricsLoading ||
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
              'usage',
              'audit',
              'broadcast',
              'analytics',
              'settings',
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
                    : tab === 'usage'
                      ? 'Usage'
                      : tab === 'audit'
                        ? 'Audit Log'
                        : tab === 'broadcast'
                          ? 'Broadcast Email'
                          : tab === 'analytics'
                            ? 'Analytics'
                            : 'Settings'}
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
          <UsersTab
            setError={setError}
            setToast={setToast}
            onLoadingChange={setUsersLoading}
            refreshNonce={usersRefreshNonce}
          />
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
            {/* Sub-tab toggle */}
            <div className="mb-5 flex items-center gap-1">
              {(['quota', 'performance'] as const).map((sub) => (
                <button
                  key={sub}
                  onClick={() => {
                    setProviderSubTab(sub);
                    const params = new URLSearchParams(window.location.search);
                    params.set('tab', 'providers');
                    params.set('sub', sub);
                    window.history.replaceState(
                      {},
                      '',
                      `${window.location.pathname}?${params.toString()}`,
                    );
                  }}
                  className={`rounded-md px-3.5 py-1.5 text-[13px] font-medium transition ${
                    providerSubTab === sub
                      ? 'bg-gray-900 text-white'
                      : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
                  }`}
                >
                  {sub === 'quota' ? 'Quota' : 'Performance'}
                </button>
              ))}
            </div>

            {/* Quota sub-tab */}
            {providerSubTab === 'quota' &&
              (providerQuotasLoading ? (
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
              ))}

            {/* Performance sub-tab */}
            {providerSubTab === 'performance' && (
              <ProviderPerformanceTab refreshKey={perfRefreshNonce} />
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
                          Source
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
                        const sourceLabel = compactUserAgent(req.user_agent);
                        const surfaceLabel = compactRequestSurface(req.request_surface);
                        return (
                          <Fragment key={req.request_id}>
                            <tr
                              className="border-b border-gray-100 hover:bg-gray-50/60 cursor-pointer transition-colors"
                              onClick={() => handleToggleRequestRow(req.request_id)}
                            >
                              <td className="py-2.5 pl-4 pr-3 text-[13px]">
                                <div className="flex items-center gap-1.5 font-medium text-gray-900">
                                  <span className="whitespace-nowrap">{req.model_id}</span>
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
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px] font-mono text-gray-500">
                                {req.user_ip ?? <span className="text-gray-300">—</span>}
                              </td>
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px] text-gray-500">
                                <div className="max-w-[180px]" title={req.user_agent || undefined}>
                                  <div className="truncate font-medium text-gray-700">
                                    {sourceLabel}
                                  </div>
                                  <div className="truncate text-[11px] text-gray-400">
                                    {surfaceLabel}
                                  </div>
                                </div>
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
                                      <span className="text-gray-500">Peer IP:</span>{' '}
                                      <span className="text-gray-700 font-mono">
                                        {req.peer_ip ?? '—'}
                                      </span>
                                    </div>
                                    <div>
                                      <span className="text-gray-500">IP source:</span>{' '}
                                      <span className="text-gray-700">{req.ip_source ?? '—'}</span>
                                    </div>
                                    <div className="col-span-full">
                                      <span className="text-gray-500">X-Forwarded-For:</span>{' '}
                                      <span className="break-words font-mono text-gray-700">
                                        {req.x_forwarded_for ?? '—'}
                                      </span>
                                    </div>
                                    <div className="col-span-full">
                                      <span className="text-gray-500">User agent:</span>{' '}
                                      <span className="break-words text-gray-700">
                                        {req.user_agent ?? '—'}
                                      </span>
                                    </div>
                                    <div>
                                      <span className="text-gray-500">Session:</span>{' '}
                                      <span className="text-gray-700 font-mono">
                                        {req.session_id ?? '—'}
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
        {activeTab === 'usage' && <TokenUsageTab perfRefreshNonce={perfRefreshNonce} />}
        {activeTab === 'settings' && <SettingsTab />}

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
    </ProtectedRoute>
  );
}
