'use client';

import Link from 'next/link';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ChangeEvent,
} from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { useAuth } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from '@/lib/api/client';

const API_BASE = config.apiBase;

interface PlaygroundProvider {
  id: string;
  name: string;
}

interface PlaygroundModel {
  id: string;
  name: string;
  provider: string;
  providers: PlaygroundProvider[];
}

interface Message {
  role: 'user' | 'assistant';
  content: string;
  reasoningContent?: string;
  durationMs?: number;
  ttftMs?: number;
  completionTokens?: number;
  modelName?: string;
}

interface PlaygroundSession {
  id: string;
  title: string;
  selectedModelId: string;
  selectedProvider: string | null;
  systemPrompt: string;
  temperature: number;
  maxTokens: number;
  messages: Message[];
  input: string;
}

function createSession(id: string, defaultModelId = ''): PlaygroundSession {
  return {
    id,
    title: 'New session',
    selectedModelId: defaultModelId,
    selectedProvider: null,
    systemPrompt: '',
    temperature: 0.7,
    maxTokens: 4096,
    messages: [],
    input: '',
  };
}

function getSessionTitle(session: PlaygroundSession): string {
  const first = session.messages.find((m) => m.role === 'user')?.content.trim();
  if (first) {
    return first.length > 40 ? `${first.slice(0, 40)}...` : first;
  }
  return session.title;
}

function getMessageCount(messages: Message[]): string {
  if (messages.length === 0) return 'No messages';
  if (messages.length === 1) return '1 message';
  return `${messages.length} messages`;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function copyToClipboard(text: string): void {
  void navigator.clipboard.writeText(text);
}

export default function PlaygroundPage() {
  const { state } = useAuth();
  const [models, setModels] = useState<PlaygroundModel[]>([]);
  const [sessions, setSessions] = useState<PlaygroundSession[]>([createSession('s-1')]);
  const [activeSessionId, setActiveSessionId] = useState('s-1');
  const [streaming, setStreaming] = useState(false);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);
  const [expandedThinking, setExpandedThinking] = useState<Set<number>>(new Set());
  const chatRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const pendingRef = useRef('');
  const pendingReasoningRef = useRef('');
  const rafRef = useRef<number | null>(null);
  const counterRef = useRef(2);
  const streamStartRef = useRef<number>(0);
  const firstTokenTimeRef = useRef<number>(0);
  const completionTokensRef = useRef<number>(0);

  const session = sessions.find((s) => s.id === activeSessionId) ?? sessions[0];
  const modelId = session?.selectedModelId || '';
  const sysPrompt = session?.systemPrompt || '';
  const temp = session?.temperature ?? 0.7;
  const maxTok = session?.maxTokens ?? 4096;
  const msgs = session?.messages || [];
  const input = session?.input || '';
  const model = models.find((m) => m.id === modelId) ?? null;
  const selectedProvider = session?.selectedProvider ?? null;
  const modelProviders = model?.providers ?? [];

  const patch = useCallback(
    (fn: (s: PlaygroundSession) => PlaygroundSession) => {
      setSessions((prev) => prev.map((s) => (s.id === activeSessionId ? fn(s) : s)));
    },
    [activeSessionId],
  );

  useEffect(() => {
    if (!hasRole(state.user?.role, 'internal')) return;
    fetchWithAuth(API_BASE, '/internal/playground/models')
      .then((r) => jsonOrThrow<{ models: PlaygroundModel[] }>(r))
      .then((d) => {
        setModels(d.models);
        if (d.models.length > 0) {
          setSessions((prev) =>
            prev.map((s) => (s.selectedModelId ? s : { ...s, selectedModelId: d.models[0].id })),
          );
        }
      })
      .catch(() => {});
  }, [state.user?.role]); // eslint-disable-line react-hooks/exhaustive-deps

  const scrollDown = useCallback((behavior: ScrollBehavior = 'auto') => {
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight, behavior });
  }, []);

  const flushDelta = useCallback(() => {
    const contentDelta = pendingRef.current;
    const reasoningDelta = pendingReasoningRef.current;
    if (!contentDelta && !reasoningDelta) return;
    pendingRef.current = '';
    pendingReasoningRef.current = '';
    patch((s) => {
      const copy = [...s.messages];
      const last = copy[copy.length - 1];
      if (!last) return s;
      copy[copy.length - 1] = {
        ...last,
        ...(contentDelta ? { content: last.content + contentDelta } : {}),
        ...(reasoningDelta
          ? { reasoningContent: (last.reasoningContent || '') + reasoningDelta }
          : {}),
      };
      return { ...s, messages: copy };
    });
  }, [patch]);

  const scheduleFlush = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      flushDelta();
      scrollDown('auto');
    });
  }, [flushDelta, scrollDown]);

  useEffect(() => {
    scrollDown(streaming ? 'auto' : 'smooth');
  }, [activeSessionId, msgs.length, scrollDown, streaming]);

  useEffect(
    () => () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    },
    [],
  );

  const autoResize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);

  const handleInputChange = useCallback(
    (e: ChangeEvent<HTMLTextAreaElement>) => {
      patch((s) => ({ ...s, input: e.target.value }));
      autoResize();
    },
    [patch, autoResize],
  );

  const resetSession = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    pendingRef.current = '';
    pendingReasoningRef.current = '';
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    setStreaming(false);
    setExpandedThinking(new Set());
    patch((s) => ({ ...s, messages: [], input: '' }));
  }, [patch]);

  const newSession = useCallback(() => {
    if (streaming) return;
    const id = `s-${counterRef.current++}`;
    const ns = createSession(id, models[0]?.id || modelId);
    setSessions((p) => [ns, ...p]);
    setActiveSessionId(id);
  }, [models, modelId, streaming]);

  const deleteSession = useCallback(
    (id: string) => {
      if (streaming) return;
      setSessions((prev) => {
        if (prev.length <= 1) return prev;
        const filtered = prev.filter((s) => s.id !== id);
        if (id === activeSessionId && filtered.length > 0) {
          setActiveSessionId(filtered[0].id);
        }
        return filtered;
      });
    },
    [activeSessionId, streaming],
  );

  const switchSession = useCallback(
    (id: string) => {
      if (streaming || id === activeSessionId) return;
      pendingRef.current = '';
      pendingReasoningRef.current = '';
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      setActiveSessionId(id);
    },
    [activeSessionId, streaming],
  );

  const handleCopy = useCallback((text: string, idx: number) => {
    copyToClipboard(text);
    setCopiedIdx(idx);
    setTimeout(() => setCopiedIdx(null), 1500);
  }, []);

  const toggleThinking = useCallback((idx: number) => {
    setExpandedThinking((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) {
        next.delete(idx);
      } else {
        next.add(idx);
      }
      return next;
    });
  }, []);

  const send = useCallback(async () => {
    if (!session) return;
    const text = session.input.trim();
    if (!text || streaming || !modelId) return;

    const userMsg: Message = { role: 'user', content: text };
    const newMsgs = [...session.messages, userMsg];
    patch((s) => ({
      ...s,
      title: getSessionTitle({ ...s, messages: newMsgs }),
      messages: [
        ...newMsgs,
        { role: 'assistant', content: '', reasoningContent: '', modelName: model?.name },
      ],
      input: '',
    }));
    setStreaming(true);
    streamStartRef.current = performance.now();
    firstTokenTimeRef.current = 0;
    completionTokensRef.current = 0;

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      const resp = await fetchWithAuth(API_BASE, '/internal/playground/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: modelId,
          system_prompt: sysPrompt,
          messages: newMsgs.map((m) => ({ role: m.role, content: m.content })),
          temperature: temp,
          max_tokens: maxTok,
          ...(selectedProvider ? { provider: selectedProvider } : {}),
        }),
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        const err = await resp.text();
        patch((s) => {
          const c = [...s.messages];
          c[c.length - 1] = { role: 'assistant', content: `Error: ${err}` };
          return { ...s, messages: c };
        });
        setStreaming(false);
        return;
      }

      const reader = resp.body?.getReader();
      if (!reader) return;

      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6).trim();
          if (payload === '[DONE]') continue;
          try {
            const parsed = JSON.parse(payload);
            if (parsed.usage?.completion_tokens) {
              completionTokensRef.current = parsed.usage.completion_tokens;
            }
            const delta = parsed.choices?.[0]?.delta;
            if (delta?.content) {
              if (!firstTokenTimeRef.current) {
                firstTokenTimeRef.current = performance.now();
              }
              pendingRef.current += delta.content;
              scheduleFlush();
            }
            if (delta?.reasoning_content) {
              pendingReasoningRef.current += delta.reasoning_content;
              scheduleFlush();
            }
          } catch {
            /* skip */
          }
        }
      }
    } catch (err) {
      flushDelta();
      if ((err as Error).name !== 'AbortError') {
        patch((s) => {
          const c = [...s.messages];
          c[c.length - 1] = { role: 'assistant', content: `Error: ${(err as Error).message}` };
          return { ...s, messages: c };
        });
      }
    } finally {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      flushDelta();
      const elapsed = Math.round(performance.now() - streamStartRef.current);
      const ttft = firstTokenTimeRef.current
        ? Math.round(firstTokenTimeRef.current - streamStartRef.current)
        : undefined;
      const tokens = completionTokensRef.current || undefined;
      patch((s) => {
        const c = [...s.messages];
        const last = c[c.length - 1];
        if (last && last.role === 'assistant') {
          const cleaned: Message = {
            ...last,
            durationMs: elapsed,
            ttftMs: ttft,
            completionTokens: tokens,
          };
          if (!cleaned.reasoningContent) {
            delete cleaned.reasoningContent;
          }
          c[c.length - 1] = cleaned;
        }
        return { ...s, messages: c };
      });
      setStreaming(false);
      abortRef.current = null;
    }
  }, [
    session,
    streaming,
    modelId,
    model?.name,
    sysPrompt,
    temp,
    maxTok,
    selectedProvider,
    patch,
    flushDelta,
    scheduleFlush,
  ]);

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      void send();
    }
  };

  if (state.loading) {
    return (
      <div className="flex h-full items-center justify-center bg-gray-950">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-700 border-t-white" />
      </div>
    );
  }

  if (!hasRole(state.user?.role, 'internal')) {
    return (
      <ProtectedRoute>
        <div className="flex h-full flex-col items-center justify-center gap-4 bg-gray-950 text-white">
          <h1 className="text-2xl font-semibold">Admin Access Required</h1>
          <p className="text-sm text-gray-400">This page is only available to administrators.</p>
          <Link
            href="/dashboard"
            className="text-sm font-medium text-indigo-400 hover:text-indigo-300"
          >
            Back to Dashboard
          </Link>
        </div>
      </ProtectedRoute>
    );
  }

  return (
    <ProtectedRoute>
      <div className="flex h-full flex-col bg-gray-950 text-white">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-gray-800 px-5">
          <div className="flex items-center gap-4">
            <Link
              href="/dashboard"
              className="text-sm font-semibold text-gray-400 transition hover:text-white"
            >
              {config.appName}
            </Link>
            <span className="text-gray-700">/</span>
            <span className="text-sm font-medium text-white">Playground</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="rounded-md bg-gray-800 px-2.5 py-1 text-xs font-medium text-gray-300">
              {model ? model.name : 'No model'}
            </span>
            <span
              className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                streaming ? 'bg-indigo-600/20 text-indigo-400' : 'bg-gray-800 text-gray-500'
              }`}
            >
              {streaming ? 'Streaming' : 'Ready'}
            </span>
          </div>
        </header>

        <div className="flex min-h-0 flex-1 overflow-hidden">
          <aside className="flex w-80 shrink-0 flex-col border-r border-gray-800 bg-gray-900">
            <div className="border-b border-gray-800 px-4 py-4">
              <div className="mb-3 flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-gray-500">
                  Sessions
                </span>
                <button
                  type="button"
                  onClick={newSession}
                  disabled={streaming}
                  className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-500 disabled:opacity-50"
                >
                  + New
                </button>
              </div>
              <div className="dark-scrollbar max-h-[200px] space-y-1 overflow-y-auto">
                {sessions.map((s) => {
                  const active = s.id === activeSessionId;
                  return (
                    <div
                      key={s.id}
                      className={`group relative rounded-lg transition ${
                        active
                          ? 'bg-gray-800 text-white'
                          : 'text-gray-400 hover:bg-gray-800/60 hover:text-gray-200'
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => switchSession(s.id)}
                        disabled={streaming && !active}
                        className="w-full px-3 py-2.5 text-left text-sm disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <div className="truncate pr-6 font-medium">{getSessionTitle(s)}</div>
                        <div className="mt-1 truncate text-xs text-gray-600">
                          {getMessageCount(s.messages)} -- {s.selectedModelId || 'no model'}
                        </div>
                      </button>
                      {sessions.length > 1 && !streaming && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteSession(s.id);
                          }}
                          className="absolute right-2 top-2.5 hidden rounded p-1 text-gray-600 transition hover:bg-gray-700 hover:text-gray-300 group-hover:block"
                          title="Delete session"
                        >
                          <svg
                            className="h-3.5 w-3.5"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              strokeWidth={2}
                              d="M6 18L18 6M6 6l12 12"
                            />
                          </svg>
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="dark-scrollbar flex-1 overflow-y-auto px-4 py-4">
              <div className="space-y-5">
                <div>
                  <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                    Model
                  </label>
                  <select
                    value={modelId}
                    onChange={(e) => {
                      const newId = e.target.value;
                      patch((s) => ({
                        ...s,
                        selectedModelId: newId,
                        selectedProvider: null,
                      }));
                    }}
                    disabled={streaming}
                    className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white outline-none transition focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 disabled:opacity-50"
                  >
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name}
                      </option>
                    ))}
                  </select>
                </div>

                {modelProviders.length > 1 && (
                  <div>
                    <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                      Provider
                    </label>
                    <select
                      value={selectedProvider ?? ''}
                      onChange={(e) =>
                        patch((s) => ({
                          ...s,
                          selectedProvider: e.target.value || null,
                        }))
                      }
                      disabled={streaming}
                      className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white outline-none transition focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 disabled:opacity-50"
                    >
                      <option value="">Auto</option>
                      {modelProviders.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </div>
                )}

                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <label className="text-xs font-semibold uppercase tracking-wider text-gray-500">
                      Temperature
                    </label>
                    <span className="text-xs tabular-nums text-gray-400">{temp.toFixed(1)}</span>
                  </div>
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    value={temp}
                    onChange={(e) =>
                      patch((s) => ({ ...s, temperature: parseFloat(e.target.value) }))
                    }
                    disabled={streaming}
                    className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-gray-700 accent-indigo-500 disabled:opacity-50"
                  />
                </div>

                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <label className="text-xs font-semibold uppercase tracking-wider text-gray-500">
                      Max Tokens
                    </label>
                    <span className="text-xs tabular-nums text-gray-400">{maxTok}</span>
                  </div>
                  <input
                    type="range"
                    min="256"
                    max="32768"
                    step="256"
                    value={maxTok}
                    onChange={(e) =>
                      patch((s) => ({ ...s, maxTokens: parseInt(e.target.value, 10) }))
                    }
                    disabled={streaming}
                    className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-gray-700 accent-indigo-500 disabled:opacity-50"
                  />
                </div>

                <div>
                  <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                    System instructions
                  </label>
                  <textarea
                    value={sysPrompt}
                    onChange={(e) => patch((s) => ({ ...s, systemPrompt: e.target.value }))}
                    placeholder="You are a research assistant..."
                    rows={6}
                    disabled={streaming}
                    className="w-full resize-none rounded-lg border border-gray-700 bg-gray-800 px-3 py-2.5 text-sm leading-6 text-white outline-none transition placeholder:text-gray-600 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 disabled:opacity-50"
                  />
                </div>

                <button
                  type="button"
                  onClick={resetSession}
                  className="w-full rounded-lg border border-gray-700 px-3 py-2 text-sm font-medium text-gray-400 transition hover:border-gray-600 hover:text-gray-200"
                >
                  Clear conversation
                </button>
              </div>
            </div>
          </aside>

          <div className="flex min-w-0 flex-1 flex-col bg-gray-950">
            <div ref={chatRef} className="dark-scrollbar flex-1 overflow-y-auto px-6 py-6">
              {msgs.length === 0 ? (
                <div className="flex h-full items-center justify-center">
                  <div className="max-w-lg text-center">
                    <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-gray-800">
                      <svg
                        className="h-6 w-6 text-gray-500"
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={1.5}
                          d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
                        />
                      </svg>
                    </div>
                    <h2 className="text-xl font-semibold text-white">Start a chat run</h2>
                    <p className="mt-2 text-sm leading-6 text-gray-500">
                      Choose a model and system prompt in the sidebar, then type a message below.
                    </p>
                  </div>
                </div>
              ) : (
                <div className="mx-auto max-w-3xl space-y-6">
                  {msgs.map((msg, i) => {
                    const isUser = msg.role === 'user';
                    const isWaiting =
                      streaming && i === msgs.length - 1 && !msg.content && !msg.reasoningContent;
                    const isCopied = copiedIdx === i;
                    const hasReasoning = !!msg.reasoningContent;
                    const isThinkingExpanded = expandedThinking.has(i);

                    return (
                      <div key={`${msg.role}-${i}`} className="group">
                        <div className="mb-1.5 flex items-center justify-between">
                          <span
                            className={`text-xs font-medium ${
                              isUser ? 'text-indigo-400' : 'text-gray-500'
                            }`}
                          >
                            {isUser ? 'You' : msg.modelName || model?.name || 'Assistant'}
                          </span>
                          <div className="flex items-center gap-2">
                            {!isUser && msg.durationMs != null && !isWaiting && (
                              <span className="text-xs tabular-nums text-gray-500">
                                {msg.ttftMs != null && `${msg.ttftMs}ms TTFT · `}
                                {msg.completionTokens != null &&
                                  msg.durationMs > 0 &&
                                  `${((msg.completionTokens / msg.durationMs) * 1000).toFixed(
                                    1,
                                  )} tok/s · `}
                                {formatDuration(msg.durationMs)}
                              </span>
                            )}
                            {msg.content && !isWaiting && (
                              <button
                                type="button"
                                onClick={() => handleCopy(msg.content, i)}
                                className="rounded p-1 text-gray-600 opacity-0 transition hover:bg-gray-800 hover:text-gray-300 group-hover:opacity-100"
                                title="Copy to clipboard"
                              >
                                {isCopied ? (
                                  <svg
                                    className="h-3.5 w-3.5 text-green-500"
                                    fill="none"
                                    viewBox="0 0 24 24"
                                    stroke="currentColor"
                                  >
                                    <path
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                      strokeWidth={2}
                                      d="M5 13l4 4L19 7"
                                    />
                                  </svg>
                                ) : (
                                  <svg
                                    className="h-3.5 w-3.5"
                                    fill="none"
                                    viewBox="0 0 24 24"
                                    stroke="currentColor"
                                  >
                                    <path
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                      strokeWidth={2}
                                      d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"
                                    />
                                  </svg>
                                )}
                              </button>
                            )}
                          </div>
                        </div>
                        <div
                          className={`rounded-2xl px-5 py-4 text-sm leading-7 ${
                            isUser
                              ? 'bg-indigo-600 text-white'
                              : 'bg-gray-900 text-gray-200 ring-1 ring-gray-800'
                          }`}
                        >
                          {isWaiting ? (
                            <div className="flex items-center gap-2.5 text-gray-500">
                              <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-700 border-t-gray-400" />
                              <span className="text-xs">Generating response...</span>
                            </div>
                          ) : (
                            <>
                              {hasReasoning && (
                                <div className="mb-3">
                                  <button
                                    type="button"
                                    onClick={() => toggleThinking(i)}
                                    className="flex items-center gap-1.5 text-xs text-gray-500 transition hover:text-gray-300"
                                  >
                                    <svg
                                      className={`h-3 w-3 transition-transform ${
                                        isThinkingExpanded ? 'rotate-90' : ''
                                      }`}
                                      fill="none"
                                      viewBox="0 0 24 24"
                                      stroke="currentColor"
                                    >
                                      <path
                                        strokeLinecap="round"
                                        strokeLinejoin="round"
                                        strokeWidth={2}
                                        d="M9 5l7 7-7 7"
                                      />
                                    </svg>
                                    <span>Thinking...</span>
                                  </button>
                                  {isThinkingExpanded && (
                                    <div className="mt-2 rounded-lg border border-gray-800 bg-gray-950 px-4 py-3 text-xs leading-6 text-gray-500 whitespace-pre-wrap">
                                      {msg.reasoningContent}
                                    </div>
                                  )}
                                </div>
                              )}
                              {isUser ? (
                                <div className="whitespace-pre-wrap">{msg.content}</div>
                              ) : (
                                <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-gray-950 prose-pre:border prose-pre:border-gray-800 prose-code:text-indigo-300 prose-a:text-indigo-400">
                                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                    {msg.content}
                                  </ReactMarkdown>
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <div className="shrink-0 border-t border-gray-800 bg-gray-950 px-6 py-4">
              <div className="mx-auto max-w-3xl">
                <div className="rounded-2xl border border-gray-800 bg-gray-900 p-3">
                  <textarea
                    ref={textareaRef}
                    value={input}
                    onChange={handleInputChange}
                    onKeyDown={onKey}
                    placeholder="Type a message... (Ctrl+Enter to send)"
                    rows={1}
                    disabled={streaming}
                    className="max-h-[200px] min-h-[44px] w-full resize-none bg-transparent px-2 py-2 text-sm leading-6 text-white outline-none placeholder:text-gray-600 disabled:opacity-50"
                  />
                  <div className="flex items-center justify-between border-t border-gray-800 px-2 pt-3">
                    <div className="text-xs text-gray-600">
                      {(() => {
                        if (!model) return 'Loading...';
                        const parts = [model.name];
                        if (selectedProvider) parts.push(selectedProvider);
                        parts.push(`temp ${temp.toFixed(1)}`);
                        parts.push(`max ${maxTok}`);
                        if (sysPrompt.trim()) parts.push('custom instructions');
                        return parts.join(' / ');
                      })()}
                    </div>
                    <div className="flex items-center gap-2">
                      {streaming && (
                        <button
                          type="button"
                          onClick={() => abortRef.current?.abort()}
                          className="rounded-lg border border-gray-700 px-3 py-1.5 text-xs font-medium text-gray-400 transition hover:border-gray-600 hover:text-white"
                        >
                          Stop
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => void send()}
                        disabled={streaming || !input.trim() || !modelId}
                        className="rounded-lg bg-indigo-600 px-4 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-500 disabled:opacity-40"
                      >
                        {streaming ? 'Sending...' : 'Send'}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </ProtectedRoute>
  );
}
