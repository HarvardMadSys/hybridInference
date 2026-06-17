'use client';

import { Fragment, useCallback, useEffect, useId, useState } from 'react';
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

function messageRefusal(m: ChatMessage): string {
  return typeof m.refusal === 'string' ? m.refusal : '';
}

function messageReasoning(m: ChatMessage): string {
  if (typeof m.reasoning_content === 'string') return m.reasoning_content;
  if (typeof m.reasoning === 'string') return m.reasoning;
  return '';
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

function formatTokens(n: number): string {
  return Math.round(n).toLocaleString();
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

  const loadRequests = useCallback(async () => {
    setReqLoading(true);
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
      toast.error(getErrorMessage(e));
    } finally {
      setReqLoading(false);
    }
  }, [reqOffset, reqUserFilter, reqModelFilter, reqErrorsOnly]);

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
      loadRequests();
      loadRequestMetrics();
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setClearingErrors(false);
    }
  }, [loadRequests, loadRequestMetrics]);

  useEffect(() => {
    loadRequests();
  }, [loadRequests]);

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
                loadRequests();
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
          onClick={handleClearErrors}
          disabled={clearingErrors}
          className="ml-auto rounded-lg border border-red-200 bg-white px-3 py-2 text-[13px] text-red-600 hover:bg-red-50 disabled:opacity-50"
        >
          {clearingErrors ? 'Clearing…' : 'Clear last hour errors'}
        </button>
        <button
          type="button"
          onClick={() => setShowExportPanel((v) => !v)}
          className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50"
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
                            <>
                              <span title="Total messages in the conversation">
                                {req.num_turns.toLocaleString()}
                              </span>
                              <span
                                className="ml-1.5 text-[10px] text-gray-400"
                                title={`${(req.num_user_turns ?? 0).toLocaleString()} user turns · ${(
                                  req.num_tool_calls ?? 0
                                ).toLocaleString()} tool calls`}
                              >
                                {(req.num_user_turns ?? 0).toLocaleString()}u ·{' '}
                                {(req.num_tool_calls ?? 0).toLocaleString()}t
                              </span>
                            </>
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
          <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
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
