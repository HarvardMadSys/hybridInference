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

/** One tool the model asked to call, and the result once the playground ran it. */
export interface ToolCall {
  id: string;
  name: string;
  /** The stream's own block index; it can skip and is not the rendered position. */
  index?: number;
  /** The tool's own name, before the wire-safe alias the model saw. */
  mcpName?: string;
  /** Arguments as the model streamed them: a JSON string, possibly partial. */
  arguments: string;
  /** False until the turn ends without a stop or error. */
  complete?: boolean;
  serverUrl?: string;
  serverHeaders?: Record<string, string>;
  status?: 'running' | 'done' | 'error';
  result?: string;
  error?: string;
}

/** The conversation in the shape every API format can be built from. */
export interface HistoryMessage {
  role: 'user' | 'assistant';
  content: string;
  toolCalls?: ToolCall[];
}

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  toolCalls?: ToolCall[];
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

/** Whether a call has a result the next request can replay. */
export function hasReplayableToolCalls(message: Message): boolean {
  return (message.toolCalls ?? []).some(
    (call) =>
      (call.status === 'done' || call.status === 'error') &&
      (typeof call.result === 'string' || typeof call.error === 'string'),
  );
}

/** A reply worth sending back as context: it finished and produced something. */
export function isUsableReply(message: Message | undefined): boolean {
  return (
    message?.role === 'assistant' &&
    message.status !== 'error' &&
    (message.content.trim() !== '' || hasReplayableToolCalls(message))
  );
}

/**
 * The conversation as the model should see it. A failed or empty turn is
 * dropped together with the question that produced it, so an error string is
 * never replayed to the model as something it said.
 */
export function requestHistory(messages: Message[]): HistoryMessage[] {
  const history: HistoryMessage[] = [];
  let user: Message | null = null;
  let started = false;
  for (const message of messages) {
    if (message.role === 'user') {
      user = message;
      started = false;
      continue;
    }
    if (!user) continue;
    if (!isUsableReply(message)) {
      // The first reply of a turn failed or said nothing: drop the question.
      // A later continuation that failed is skipped, keeping the replies before it.
      if (!started) user = null;
      continue;
    }
    if (!started) {
      history.push({ role: 'user', content: user.content });
      started = true;
    }
    history.push({
      role: 'assistant',
      content: message.content,
      ...(message.toolCalls?.length ? { toolCalls: message.toolCalls } : {}),
    });
  }
  if (user && !started) history.push({ role: 'user', content: user.content });
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
