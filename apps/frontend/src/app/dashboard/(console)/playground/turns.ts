/** Pure helpers for the Playground's chat turns, kept apart so they can be unit-tested. */

export interface RoutingAttempt {
  provider: string;
  endpointId: string;
  errorType: string;
}

/** Which upstream actually served a turn — from the `_playground_route` frame. */
export interface RoutingInfo {
  provider: string;
  endpointId: string;
  host?: string;
  fallback?: boolean;
  failedAttempts?: RoutingAttempt[];
}

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoningContent?: string;
  durationMs?: number;
  /** Time to the first streamed token, reasoning or answer. */
  ttftMs?: number;
  /** Time spent streaming reasoning before the first answer token. */
  reasoningMs?: number;
  completionTokens?: number;
  finishReason?: string;
  /** Why the turn did not complete; any partial content is kept alongside. */
  status?: 'error' | 'aborted';
  error?: string;
  modelName?: string;
  routing?: RoutingInfo;
}

/** A reply worth sending back as context: it finished and said something. */
export function isUsableReply(message: Message | undefined): boolean {
  return (
    message?.role === 'assistant' && message.status !== 'error' && message.content.trim() !== ''
  );
}

/**
 * The conversation as the model should see it. A failed or empty turn is
 * dropped together with the question that produced it, so an error string is
 * never replayed to the model as something it said.
 */
export function requestHistory(messages: Message[]): { role: Message['role']; content: string }[] {
  const history: { role: Message['role']; content: string }[] = [];
  for (let i = 0; i < messages.length; i++) {
    const message = messages[i];
    if (message.role !== 'user') continue;
    const reply = messages[i + 1];
    if (reply?.role === 'assistant') {
      if (isUsableReply(reply)) {
        history.push({ role: 'user', content: message.content });
        history.push({ role: 'assistant', content: reply.content });
      }
      i++;
    } else {
      history.push({ role: 'user', content: message.content });
    }
  }
  return history;
}

/** Below this, the reply arrived in one burst (a buffering proxy) and has no measurable rate. */
const MIN_DECODE_MS = 20;

/** Generation speed after the first token, so queueing and TTFT don't dilute it. */
export function decodeRate(message: Message): number | null {
  if (message.completionTokens == null || message.durationMs == null) return null;
  const decodeMs = message.durationMs - (message.ttftMs ?? 0);
  return decodeMs >= MIN_DECODE_MS ? (message.completionTokens / decodeMs) * 1000 : null;
}

export function finishNote(message: Message): string | null {
  if (message.finishReason === 'length') {
    return message.content.trim()
      ? 'Cut off at the Max Tokens limit. Raise it and retry for the full answer.'
      : 'The model spent the whole Max Tokens budget reasoning and never answered. Raise Max Tokens and retry.';
  }
  if (message.finishReason === 'content_filter') return "Stopped by the provider's content filter.";
  return null;
}
