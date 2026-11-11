/** Pure request, stream and code helpers for the Playground's API selector. */

import type { Message, RoutingInfo } from './turns';

export type PlaygroundApi = 'chat' | 'responses' | 'messages';

export const API_OPTIONS: { value: PlaygroundApi; label: string }[] = [
  { value: 'chat', label: 'Chat Completions' },
  { value: 'responses', label: 'Responses' },
  { value: 'messages', label: 'Anthropic Messages' },
];

export type CodeLanguage = 'python' | 'curl';

export const CODE_LANGUAGES: { value: CodeLanguage; label: string }[] = [
  { value: 'python', label: 'Python' },
  { value: 'curl', label: 'cURL' },
];

const ENDPOINTS: Record<PlaygroundApi, string> = {
  chat: '/internal/playground/chat',
  responses: '/internal/playground/responses',
  messages: '/internal/playground/messages',
};

export function endpointFor(api: PlaygroundApi): string {
  return ENDPOINTS[api];
}

export interface RequestOptions {
  model: string;
  history: { role: Message['role']; content: string }[];
  systemPrompt: string;
  temperature: number;
  maxTokens: number;
  provider: string | null;
}

/** The body each API format expects, from the same session state. */
export function buildRequest(api: PlaygroundApi, options: RequestOptions): Record<string, unknown> {
  const { model, history, systemPrompt, temperature, maxTokens, provider } = options;
  if (api === 'responses') {
    return {
      model,
      ...(systemPrompt.trim() ? { instructions: systemPrompt.trim() } : {}),
      input: history,
      temperature,
      max_output_tokens: maxTokens,
      stream: true,
    };
  }
  if (api === 'messages') {
    return {
      model,
      ...(systemPrompt.trim() ? { system: systemPrompt.trim() } : {}),
      messages: history,
      temperature,
      max_tokens: maxTokens,
      stream: true,
    };
  }
  return {
    model,
    system_prompt: systemPrompt,
    messages: history,
    temperature,
    max_tokens: maxTokens,
    ...(provider ? { provider } : {}),
  };
}

export interface StreamEvent {
  content?: string;
  reasoning?: string;
  tokens?: number;
  finishReason?: string;
  error?: string;
  routing?: RoutingInfo;
}

function errorMessage(raw: unknown): string | null {
  if (!raw) return null;
  if (typeof raw === 'string') return raw;
  if (typeof raw === 'object') {
    const message = (raw as Record<string, unknown>).message;
    if (typeof message === 'string' && message) return message;
  }
  return 'The upstream reported an error.';
}

function routingInfo(raw: unknown): RoutingInfo | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const endpointId = typeof r.endpoint_id === 'string' ? r.endpoint_id : '';
  if (!endpointId) return null;
  const rawAttempts = Array.isArray(r.failed_attempts) ? r.failed_attempts : [];
  const failedAttempts = rawAttempts
    .filter(
      (attempt): attempt is Record<string, unknown> => !!attempt && typeof attempt === 'object',
    )
    .map((attempt) => ({
      provider: typeof attempt.provider === 'string' ? attempt.provider : '',
      endpointId:
        typeof attempt.endpoint_id === 'string'
          ? attempt.endpoint_id
          : typeof attempt.provider === 'string'
            ? attempt.provider
            : '',
      errorType: typeof attempt.error_type === 'string' ? attempt.error_type : '',
    }));
  return {
    provider: typeof r.provider === 'string' ? r.provider : '',
    endpointId,
    ...(typeof r.host === 'string' && r.host ? { host: r.host } : {}),
    ...(r.fallback === true ? { fallback: true } : {}),
    ...(failedAttempts.length > 0 ? { failedAttempts } : {}),
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

/** Normalize one decoded SSE payload, whatever API format produced it. */
export function parseStreamEvent(api: PlaygroundApi, parsed: unknown): StreamEvent | null {
  const event = asRecord(parsed);
  if (Object.keys(event).length === 0) return null;
  const result: StreamEvent = {};

  if (api === 'chat') {
    const error = errorMessage(event.error);
    if (error) result.error = error;
    const routing = routingInfo(event._playground_route);
    if (routing) result.routing = routing;
    const usage = asRecord(event.usage);
    if (typeof usage.completion_tokens === 'number' && usage.completion_tokens) {
      result.tokens = usage.completion_tokens;
    }
    const choices = Array.isArray(event.choices) ? event.choices : [];
    const choice = asRecord(choices[0]);
    if (typeof choice.finish_reason === 'string' && choice.finish_reason) {
      result.finishReason = choice.finish_reason;
    }
    const delta = asRecord(choice.delta);
    if (typeof delta.content === 'string') result.content = delta.content;
    if (typeof delta.reasoning_content === 'string') result.reasoning = delta.reasoning_content;
  } else if (api === 'responses') {
    const response = asRecord(event.response);
    if (event.type === 'response.output_text.delta' && typeof event.delta === 'string') {
      result.content = event.delta;
    } else if (event.type === 'response.completed' || event.type === 'response.incomplete') {
      const usage = asRecord(response.usage);
      if (typeof usage.output_tokens === 'number' && usage.output_tokens) {
        result.tokens = usage.output_tokens;
      }
      if (typeof response.status === 'string') result.finishReason = response.status;
    } else if (event.type === 'response.failed') {
      result.error = errorMessage(response.error) ?? 'The upstream reported an error.';
    } else if (event.type === 'error') {
      result.error = errorMessage(event) ?? 'The upstream reported an error.';
    }
  } else {
    const delta = asRecord(event.delta);
    const usage = asRecord(event.usage);
    if (event.type === 'content_block_delta') {
      if (delta.type === 'text_delta' && typeof delta.text === 'string') {
        result.content = delta.text;
      } else if (delta.type === 'thinking_delta' && typeof delta.thinking === 'string') {
        result.reasoning = delta.thinking;
      }
    } else if (event.type === 'message_delta') {
      if (typeof usage.output_tokens === 'number' && usage.output_tokens) {
        result.tokens = usage.output_tokens;
      }
      if (typeof delta.stop_reason === 'string' && delta.stop_reason) {
        result.finishReason = delta.stop_reason;
      }
    } else if (event.type === 'error') {
      result.error = errorMessage(event.error) ?? 'The upstream reported an error.';
    }
  }

  return Object.keys(result).length > 0 ? result : null;
}

function singleQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function pyMessages(history: RequestOptions['history'], systemPrompt: string): string[] {
  const lines = history.map(
    (message) =>
      `        {"role": ${JSON.stringify(message.role)}, "content": ${JSON.stringify(message.content)}},`,
  );
  if (systemPrompt.trim()) {
    lines.unshift(`        {"role": "system", "content": ${JSON.stringify(systemPrompt.trim())}},`);
  }
  if (lines.length === 0) return ['    messages=[],'];
  return ['    messages=[', ...lines, '    ],'];
}

/** A runnable Python call with the official SDK, for the selected API format. */
export function buildPythonCode(
  api: PlaygroundApi,
  baseUrl: string,
  options: RequestOptions,
): string {
  const base = baseUrl.replace(/\/+$/, '');
  if (api === 'messages') {
    const system = options.systemPrompt.trim()
      ? [`    system=${JSON.stringify(options.systemPrompt.trim())},`]
      : [];
    return [
      'import os',
      '',
      'from anthropic import Anthropic',
      '',
      'client = Anthropic(',
      '    api_key=os.environ["HYBRID_API_KEY"],',
      `    base_url=${JSON.stringify(base)},`,
      ')',
      '',
      'stream = client.messages.create(',
      `    model=${JSON.stringify(options.model)},`,
      `    max_tokens=${options.maxTokens},`,
      `    temperature=${options.temperature},`,
      ...system,
      ...pyMessages(options.history, ''),
      '    stream=True,',
      ')',
      '',
      'for event in stream:',
      '    if event.type == "content_block_delta" and event.delta.type == "text_delta":',
      '        print(event.delta.text, end="")',
    ].join('\n');
  }

  const call =
    api === 'responses'
      ? [
          'stream = client.responses.create(',
          `    model=${JSON.stringify(options.model)},`,
          ...(options.systemPrompt.trim()
            ? [`    instructions=${JSON.stringify(options.systemPrompt.trim())},`]
            : []),
          '    input=[',
          ...options.history.map(
            (message) =>
              `        {"role": ${JSON.stringify(message.role)}, "content": ${JSON.stringify(message.content)}},`,
          ),
          '    ],',
          `    temperature=${options.temperature},`,
          `    max_output_tokens=${options.maxTokens},`,
          '    stream=True,',
          ')',
          '',
          'for event in stream:',
          '    if event.type == "response.output_text.delta":',
          '        print(event.delta, end="")',
        ]
      : [
          'stream = client.chat.completions.create(',
          `    model=${JSON.stringify(options.model)},`,
          ...pyMessages(options.history, options.systemPrompt),
          `    temperature=${options.temperature},`,
          `    max_tokens=${options.maxTokens},`,
          '    stream=True,',
          ')',
          '',
          'for chunk in stream:',
          '    print(chunk.choices[0].delta.content or "", end="")',
        ];
  return [
    'import os',
    '',
    'from openai import OpenAI',
    '',
    'client = OpenAI(',
    '    api_key=os.environ["HYBRID_API_KEY"],',
    `    base_url=${JSON.stringify(`${base}/v1`)},`,
    ')',
    '',
    ...call,
  ].join('\n');
}

/** The equivalent public-API call in the selected code language. */
export function buildCode(
  language: CodeLanguage,
  api: PlaygroundApi,
  baseUrl: string,
  options: RequestOptions,
): string {
  if (language === 'python') return buildPythonCode(api, baseUrl, options);
  const base = baseUrl.replace(/\/+$/, '');
  const body = buildRequest(api, { ...options, provider: null });
  const json = JSON.stringify(body, null, 2);
  if (api === 'messages') {
    return [
      `curl ${base}/v1/messages \\`,
      `  -H "x-api-key: $HYBRID_API_KEY" \\`,
      `  -H "anthropic-version: 2023-06-01" \\`,
      `  -H "Content-Type: application/json" \\`,
      `  -d ${singleQuote(json)}`,
    ].join('\n');
  }
  const path = api === 'responses' ? '/v1/responses' : '/v1/chat/completions';
  return [
    `curl ${base}${path} \\`,
    `  -H "Authorization: Bearer $HYBRID_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d ${singleQuote(json)}`,
  ].join('\n');
}
