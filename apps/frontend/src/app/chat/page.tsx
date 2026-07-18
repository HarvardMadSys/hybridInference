'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { Button } from '@/components/ui/Button';
import { Markdown } from '@/components/ui/Markdown';
import { streamRagChat, type RagSource } from '@/lib/api/chat';
import { APIError } from '@/lib/utils/errors';
import { branding } from '@/config/branding';
import { useBranding, useSiteConfig } from '@/components/providers/SiteConfigProvider';

interface UiMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: RagSource[];
  streaming?: boolean;
}

function docUrl(source: string): string {
  // The public docs are a Sphinx site; a page named `quickstart.md` builds to
  // `quickstart.html`. Best-effort deep link — falls back to a readable label.
  return `${branding.docsUrl}${source.replace(/\.md$/, '.html')}`;
}

function updateLast(messages: UiMessage[], patch: Partial<UiMessage>): UiMessage[] {
  if (messages.length === 0) return messages;
  const next = messages.slice();
  next[next.length - 1] = { ...next[next.length - 1], ...patch };
  return next;
}

function SourceChips({ sources }: { sources: RagSource[] }) {
  if (!sources.length) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-2 border-t border-gray-100 pt-3">
      <span className="text-xs font-medium text-gray-400">Sources</span>
      {sources.map((s) => (
        <a
          key={s.id}
          href={docUrl(s.source)}
          target="_blank"
          rel="noopener noreferrer"
          title={s.title}
          className="max-w-full truncate rounded-full bg-gray-100 px-2.5 py-1 text-xs text-gray-600 transition-colors hover:bg-crimson/10 hover:text-crimson"
        >
          [{s.n}] {s.title}
        </a>
      ))}
    </div>
  );
}

function ChatView() {
  const runtimeBranding = useBranding();
  const exampleQuestions = [
    'How do I get an API key?',
    'Which models can I use for coding?',
    `How do I set up Cursor with ${runtimeBranding.appName}?`,
    'What request headers does the API accept?',
  ];
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || isStreaming) return;
      setError(null);
      setInput('');

      const history = messages
        .filter((m) => !m.streaming)
        .map((m) => ({ role: m.role, content: m.content }));

      setMessages((prev) => [
        ...prev,
        { role: 'user', content: question },
        { role: 'assistant', content: '', streaming: true },
      ]);
      setIsStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;
      let acc = '';

      try {
        await streamRagChat({
          messages: [...history, { role: 'user', content: question }],
          signal: controller.signal,
          onSources: (sources) => setMessages((prev) => updateLast(prev, { sources })),
          onToken: (delta) => {
            acc += delta;
            setMessages((prev) => updateLast(prev, { content: acc }));
          },
        });
      } catch (err) {
        if (!(err instanceof DOMException && err.name === 'AbortError')) {
          const message =
            err instanceof APIError
              ? err.message || 'Something went wrong. Please try again.'
              : 'Something went wrong. Please try again.';
          setError(message);
          if (!acc) {
            setMessages((prev) => prev.slice(0, -1)); // drop empty assistant bubble
          }
        }
      } finally {
        setMessages((prev) => updateLast(prev, { streaming: false }));
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    [isStreaming, messages],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const isEmpty = messages.length === 0;

  return (
    <div className="flex h-[calc(100vh-14rem)] min-h-[28rem] flex-col">
      <div className="mb-4">
        <h1 className="text-2xl font-bold tracking-tight">Docs Assistant</h1>
        <p className="mt-1 text-sm text-gray-500">
          Ask anything about {runtimeBranding.appName}. Answers are grounded in the{' '}
          <a
            href={branding.docsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-crimson hover:underline"
          >
            official documentation
          </a>
          .
        </p>
      </div>

      <div
        ref={scrollRef}
        className="flex-1 space-y-4 overflow-y-auto rounded-xl border border-gray-200 bg-gray-50/50 p-4"
      >
        {isEmpty ? (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <p className="text-sm text-gray-500">Try one of these to get started:</p>
            <div className="flex max-w-md flex-wrap justify-center gap-2">
              {exampleQuestions.map((q) => (
                <button
                  key={q}
                  type="button"
                  onClick={() => send(q)}
                  className="rounded-full border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-700 transition-colors hover:border-crimson hover:text-crimson"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
              <div
                className={
                  m.role === 'user'
                    ? 'max-w-[85%] rounded-2xl rounded-br-sm bg-black px-4 py-2.5 text-sm text-white'
                    : 'max-w-[85%] rounded-2xl rounded-bl-sm bg-white px-4 py-3 text-sm text-gray-800 shadow-sm ring-1 ring-gray-200/70'
                }
              >
                {m.role === 'user' ? (
                  <div className="whitespace-pre-wrap break-words">{m.content}</div>
                ) : m.content ? (
                  <Markdown text={m.content} />
                ) : m.streaming ? (
                  <span className="text-gray-400">Thinking…</span>
                ) : null}
                {m.role === 'assistant' && m.sources && <SourceChips sources={m.sources} />}
              </div>
            </div>
          ))
        )}
      </div>

      {error && (
        <p className="mt-2 text-sm text-red-600" role="alert">
          {error}
        </p>
      )}

      <form
        className="mt-4 flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send(input);
            }
          }}
          placeholder={`Ask a question about ${runtimeBranding.appName}…`}
          rows={1}
          className="max-h-40 min-h-[2.75rem] flex-1 resize-none rounded-lg border border-gray-300 px-3 py-2.5 text-sm focus:border-black focus:outline-none focus:ring-1 focus:ring-black"
        />
        {isStreaming ? (
          <Button type="button" variant="secondary" onClick={stop}>
            Stop
          </Button>
        ) : (
          <Button type="submit" disabled={!input.trim()}>
            Send
          </Button>
        )}
      </form>
    </div>
  );
}

export default function ChatPage() {
  const { features } = useSiteConfig();
  return (
    <ProtectedRoute>
      {features.rag ? (
        <ChatView />
      ) : (
        <div className="mx-auto w-full max-w-xl rounded-xl border border-gray-200 bg-white p-8 text-center text-gray-600 shadow-sm">
          The documentation assistant is not enabled for this distribution.
        </div>
      )}
    </ProtectedRoute>
  );
}
