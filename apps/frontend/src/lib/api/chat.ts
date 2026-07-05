// Streaming client for the docs RAG assistant (`POST /v1/rag/chat`).
//
// The backend streams Server-Sent Events: first a `{type:"sources"}` event with
// the retrieved citations, then OpenAI-format completion chunks, then `[DONE]`.

import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from '@/lib/api/client';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface RagSource {
  n: number;
  id: string;
  source: string;
  title: string;
  score: number;
}

export interface StreamRagChatOptions {
  messages: ChatMessage[];
  model?: string;
  topK?: number;
  signal?: AbortSignal;
  onSources?: (sources: RagSource[]) => void;
  onToken?: (delta: string) => void;
}

function handleEvent(raw: string, opts: StreamRagChatOptions): boolean {
  // Returns true when the stream signalled completion ([DONE]).
  const line = raw
    .split('\n')
    .find((l) => l.startsWith('data:'));
  if (!line) return false;

  const payload = line.slice(5).trim();
  if (payload === '' ) return false;
  if (payload === '[DONE]') return true;

  try {
    const obj = JSON.parse(payload);
    if (obj.type === 'sources' && Array.isArray(obj.sources)) {
      opts.onSources?.(obj.sources as RagSource[]);
      return false;
    }
    const delta = obj?.choices?.[0]?.delta?.content;
    if (typeof delta === 'string' && delta.length > 0) {
      opts.onToken?.(delta);
    }
  } catch {
    // Ignore keep-alive/comment lines and any non-JSON noise.
  }
  return false;
}

export async function streamRagChat(opts: StreamRagChatOptions): Promise<void> {
  const resp = await fetchWithAuth(config.apiBase, '/v1/rag/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      messages: opts.messages,
      model: opts.model,
      top_k: opts.topK,
      stream: true,
    }),
    signal: opts.signal,
  });

  if (!resp.ok || !resp.body) {
    // Reuse the shared error mapper for a typed APIError with a useful message.
    await jsonOrThrow(resp);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep = buffer.indexOf('\n\n');
    while (sep !== -1) {
      const event = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      if (handleEvent(event, opts)) return;
      sep = buffer.indexOf('\n\n');
    }
  }

  // Flush any trailing event without a terminating blank line.
  if (buffer.trim()) handleEvent(buffer, opts);
}
