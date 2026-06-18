'use client';

// Shared rendering for admin request/response payloads (prompt, response,
// reasoning). Renders OpenAI chat-completions shapes and the Anthropic
// Messages shape used by Claude Code, including `tool_use` / `tool_result`
// content blocks. Imported by RequestsTab and unit-tested directly.

import { Fragment, useState } from 'react';

export function previewText(value: string, maxChars: number = 280): string {
  if (value.length <= maxChars) return value;
  return `${value.slice(0, maxChars)}...`;
}

export type ParseResult = { ok: true; value: unknown } | { ok: false };

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

export type ChatMessage = {
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

type AnthropicToolUse = { id?: unknown; name?: unknown; input?: unknown };
type AnthropicToolResult = { tool_use_id?: unknown; content?: unknown; is_error?: unknown };

function contentBlocks(content: unknown): Record<string, unknown>[] {
  if (!Array.isArray(content)) return [];
  return content.filter(isRecord);
}

function getToolUseBlocks(message: ChatMessage): AnthropicToolUse[] {
  return contentBlocks(message.content).filter((b) => b.type === 'tool_use');
}

function getToolResultBlocks(message: ChatMessage): AnthropicToolResult[] {
  return contentBlocks(message.content).filter((b) => b.type === 'tool_result');
}

function toolUseName(tu: AnthropicToolUse): string {
  return typeof tu.name === 'string' ? tu.name : '';
}

function jsonPretty(value: unknown): string {
  if (typeof value === 'string') {
    const parsed = tryParseJson(value);
    return parsed.ok ? JSON.stringify(parsed.value, null, 2) : value;
  }
  if (value == null) return '';
  return JSON.stringify(value, null, 2);
}

function toolResultText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === 'string') return part;
        if (isRecord(part)) {
          if (part.type === 'text' && typeof part.text === 'string') return part.text;
          return JSON.stringify(part, null, 2);
        }
        return '';
      })
      .filter((s) => s.length > 0)
      .join('\n');
  }
  if (content == null) return '';
  return JSON.stringify(content, null, 2);
}

export function flattenContent(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === 'string') return part;
        if (isRecord(part)) {
          if (part.type === 'text' && typeof part.text === 'string') return part.text;
          // tool_use / tool_result blocks are rendered separately with full
          // detail (name, input, result), so skip them in the text flatten.
          if (part.type === 'tool_use' || part.type === 'tool_result') return '';
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

export function MessageBlock({ message }: { message: ChatMessage }) {
  const text = flattenContent(message.content);
  const reasoning = messageReasoning(message);
  const refusal = messageRefusal(message);
  const toolCalls = getToolCalls(message);
  const toolUseBlocks = getToolUseBlocks(message);
  const toolResultBlocks = getToolResultBlocks(message);
  const toolCallId = typeof message.tool_call_id === 'string' ? message.tool_call_id : '';
  const toolName = typeof message.name === 'string' ? message.name : '';
  const showToolMeta = message.role === 'tool' && (toolCallId || toolName);
  const rendered =
    text.length > 0 ||
    reasoning.length > 0 ||
    refusal.length > 0 ||
    toolCalls.length > 0 ||
    toolUseBlocks.length > 0 ||
    toolResultBlocks.length > 0 ||
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
      {toolUseBlocks.length > 0 && (
        <div className="mt-1 space-y-1">
          {toolUseBlocks.map((tu, i) => {
            const name = toolUseName(tu);
            const id = typeof tu.id === 'string' ? tu.id : '';
            const input = jsonPretty(tu.input);
            return (
              <div
                key={id || `${i}-${name}`}
                className="rounded-md border border-gray-200 bg-white px-2 py-1"
              >
                <div className="mb-0.5 flex flex-wrap items-center gap-x-2 text-[10px]">
                  <span className="font-medium uppercase tracking-wide text-gray-500">
                    tool_use
                  </span>
                  {name && <span className="font-mono text-gray-700">{name}</span>}
                  {id && <span className="font-mono text-gray-400">{id}</span>}
                </div>
                {input.length > 0 && (
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] text-gray-700">
                    {input}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
      {toolResultBlocks.length > 0 && (
        <div className="mt-1 space-y-1">
          {toolResultBlocks.map((tr, i) => {
            const id = typeof tr.tool_use_id === 'string' ? tr.tool_use_id : '';
            const isError = tr.is_error === true;
            const out = toolResultText(tr.content);
            return (
              <div
                key={id || `${i}`}
                className={`rounded-md border bg-white px-2 py-1 ${
                  isError ? 'border-red-200' : 'border-gray-200'
                }`}
              >
                <div className="mb-0.5 flex flex-wrap items-center gap-x-2 text-[10px]">
                  <span
                    className={`font-medium uppercase tracking-wide ${
                      isError ? 'text-red-600' : 'text-gray-500'
                    }`}
                  >
                    tool_result{isError ? ' (error)' : ''}
                  </span>
                  {id && <span className="font-mono text-gray-400">{id}</span>}
                </div>
                {out.length > 0 && (
                  <pre
                    className={`overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] ${
                      isError ? 'text-red-700' : 'text-gray-700'
                    }`}
                  >
                    {out}
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

export function JsonChatView({ data }: { data: unknown }) {
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
  const toolUses = getToolUseBlocks(message);
  if (calls.length === 0 && toolUses.length === 0) return '';
  const names = [...calls.map(toolCallName), ...toolUses.map(toolUseName)].filter(
    (n) => n.length > 0,
  );
  return names.length > 0 ? `[tool_calls: ${names.join(', ')}]` : '[tool_calls]';
}

function toolResultsPreview(message: ChatMessage): string {
  const results = getToolResultBlocks(message);
  if (results.length === 0) return '';
  const ids = results
    .map((tr) => (typeof tr.tool_use_id === 'string' ? tr.tool_use_id : ''))
    .filter((id) => id.length > 0);
  return ids.length > 0 ? `[tool_results: ${ids.join(', ')}]` : '[tool_results]';
}

function previewFromMessages(messages: ChatMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === 'user') {
      const text = flattenContent(m.content);
      if (text) return previewText(text);
      const tr = toolResultsPreview(m);
      if (tr) return previewText(tr);
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
    const tr = toolResultsPreview(last);
    if (tr) return previewText(tr);
    const refusal = messageRefusal(last);
    if (refusal) return previewText(`[refusal] ${refusal}`);
    const reasoning = messageReasoning(last);
    if (reasoning) return previewText(`[reasoning] ${reasoning}`);
  }
  return null;
}

export function computePreview(parsed: unknown, fallback: string): string {
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

export function FoldedText({ label, value }: { label: string; value?: string | null }) {
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
