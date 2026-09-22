'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { Button } from '@/components/ui/Button';
import { Markdown } from '@/components/ui/Markdown';
import { streamRagChat, type RagSource } from '@/lib/api/chat';
import { APIError } from '@/lib/utils/errors';
import { useBranding, useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';

interface UiMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: RagSource[];
  streaming?: boolean;
}

function docUrl(source: string, docsUrl: string): string {
  // The public docs are a Sphinx site; a page named `quickstart.md` builds to
  // `quickstart.html`. Best-effort deep link — falls back to a readable label.
  const docsBase = docsUrl.replace(/\/+$/, '');
  const docPath = source.replace(/^\/+/, '').replace(/\.md$/, '.html');
  return `${docsBase}/${docPath}`;
}

function updateLast(messages: UiMessage[], patch: Partial<UiMessage>): UiMessage[] {
  if (messages.length === 0) return messages;
  const next = messages.slice();
  next[next.length - 1] = { ...next[next.length - 1], ...patch };
  return next;
}

function SourceChips({ sources, docsUrl }: { sources: RagSource[]; docsUrl: string }) {
  const t = useT();
  if (!sources.length) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-2 border-t border-gray-100 pt-3">
      <span className="text-xs font-medium text-gray-400">
        {t('chat.sources_label', 'Sources')}
      </span>
      {sources.map((s) => (
        <a
          key={s.id}
          href={docUrl(s.source, docsUrl)}
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
  const t = useT();
  const runtimeBranding = useBranding();
  const exampleQuestions = [
    t('chat.example_1', 'How do I get an API key?'),
    t('chat.example_2', 'Which models can I use for coding?'),
    fill(t('chat.example_3', 'How do I set up Cursor with {app_name}?'), {
      app_name: runtimeBranding.appName,
    }),
    t('chat.example_4', 'What request headers does the API accept?'),
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
              ? err.message || t('chat.error_generic', 'Something went wrong. Please try again.')
              : t('chat.error_generic', 'Something went wrong. Please try again.');
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
    [isStreaming, messages, t],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const isEmpty = messages.length === 0;

  return (
    <div className="flex h-[calc(100vh-14rem)] min-h-[28rem] flex-col">
      <div className="mb-4">
        <h1 className="text-2xl font-bold tracking-tight">{t('chat.title', 'Docs Assistant')}</h1>
        <p className="mt-1 text-sm text-gray-500">
          {fill(t('chat.subtitle_lead', 'Ask anything about {app_name}.'), {
            app_name: runtimeBranding.appName,
          })}{' '}
          {t('chat.subtitle_grounded', 'Answers are grounded in the')}{' '}
          {runtimeBranding.docsUrl ? (
            <a
              href={runtimeBranding.docsUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-crimson hover:underline"
            >
              {t('chat.official_docs', 'official documentation')}
            </a>
          ) : (
            t('chat.official_docs', 'official documentation')
          )}
          .
        </p>
      </div>

      <div
        ref={scrollRef}
        className="flex-1 space-y-4 overflow-y-auto rounded-xl border border-gray-200 bg-gray-50/50 p-4"
      >
        {isEmpty ? (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <p className="text-sm text-gray-500">
              {t('chat.empty_hint', 'Try one of these to get started:')}
            </p>
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
                  <span className="text-gray-400">{t('chat.thinking', 'Thinking…')}</span>
                ) : null}
                {m.role === 'assistant' && m.sources && (
                  <SourceChips sources={m.sources} docsUrl={runtimeBranding.docsUrl} />
                )}
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
          placeholder={fill(t('chat.input_placeholder', 'Ask a question about {app_name}…'), {
            app_name: runtimeBranding.appName,
          })}
          rows={1}
          className="max-h-40 min-h-[2.75rem] flex-1 resize-none rounded-lg border border-gray-300 px-3 py-2.5 text-sm focus:border-black focus:outline-none focus:ring-1 focus:ring-black"
        />
        {isStreaming ? (
          <Button type="button" variant="secondary" onClick={stop}>
            {t('chat.stop', 'Stop')}
          </Button>
        ) : (
          <Button type="submit" disabled={!input.trim()}>
            {t('chat.send', 'Send')}
          </Button>
        )}
      </form>
    </div>
  );
}

export default function ChatPage() {
  const t = useT();
  const { features } = useSiteConfig();
  return (
    <ProtectedRoute>
      {features.rag ? (
        <ChatView />
      ) : (
        <div className="mx-auto w-full max-w-xl rounded-xl border border-gray-200 bg-white p-8 text-center text-gray-600 shadow-sm">
          {t(
            'chat.unavailable',
            'The documentation assistant is not enabled for this distribution.',
          )}
        </div>
      )}
    </ProtectedRoute>
  );
}
