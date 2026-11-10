import { describe, expect, it } from 'vitest';

import { buildCode, buildRequest, endpointFor, parseStreamEvent, type RequestOptions } from './api';

const OPTIONS: RequestOptions = {
  model: 'glm-4.6',
  history: [
    { role: 'user', content: 'hi' },
    { role: 'assistant', content: 'hello' },
  ],
  systemPrompt: 'Be terse.',
  temperature: 0.5,
  maxTokens: 512,
  provider: 'glm-4.6:zai-api',
};

describe('endpointFor', () => {
  it('routes each format to its own internal endpoint', () => {
    expect(endpointFor('chat')).toBe('/internal/playground/chat');
    expect(endpointFor('responses')).toBe('/internal/playground/responses');
    expect(endpointFor('messages')).toBe('/internal/playground/messages');
  });
});

describe('buildRequest', () => {
  it('keeps the chat playground request shape, including the provider pin', () => {
    expect(buildRequest('chat', OPTIONS)).toEqual({
      model: 'glm-4.6',
      system_prompt: 'Be terse.',
      messages: OPTIONS.history,
      temperature: 0.5,
      max_tokens: 512,
      provider: 'glm-4.6:zai-api',
    });
  });

  it('moves the system prompt to instructions for Responses and never pins a route', () => {
    expect(buildRequest('responses', OPTIONS)).toEqual({
      model: 'glm-4.6',
      instructions: 'Be terse.',
      input: OPTIONS.history,
      temperature: 0.5,
      max_output_tokens: 512,
      stream: true,
    });
  });

  it('uses the Anthropic Messages field names', () => {
    expect(buildRequest('messages', OPTIONS)).toEqual({
      model: 'glm-4.6',
      system: 'Be terse.',
      messages: OPTIONS.history,
      temperature: 0.5,
      max_tokens: 512,
      stream: true,
    });
  });

  it('omits an empty system prompt instead of sending an empty one', () => {
    const options = { ...OPTIONS, systemPrompt: '  ' };
    expect(buildRequest('responses', options)).not.toHaveProperty('instructions');
    expect(buildRequest('messages', options)).not.toHaveProperty('system');
  });
});

describe('parseStreamEvent', () => {
  it('reads chat content, reasoning, usage and routing', () => {
    expect(
      parseStreamEvent('chat', {
        choices: [
          { index: 0, delta: { content: 'Hi', reasoning_content: 'hmm' }, finish_reason: 'stop' },
        ],
        usage: { completion_tokens: 7 },
        _playground_route: {
          provider: 'zai',
          endpoint_id: 'glm-4.6:zai-api',
          fallback: true,
        },
      }),
    ).toEqual({
      content: 'Hi',
      reasoning: 'hmm',
      tokens: 7,
      finishReason: 'stop',
      routing: {
        provider: 'zai',
        endpointId: 'glm-4.6:zai-api',
        fallback: true,
      },
    });
  });

  it('reads Responses text deltas and the terminal usage', () => {
    expect(
      parseStreamEvent('responses', {
        type: 'response.output_text.delta',
        delta: 'Hello',
      }),
    ).toEqual({ content: 'Hello' });
    expect(
      parseStreamEvent('responses', {
        type: 'response.completed',
        response: { status: 'completed', usage: { output_tokens: 12 } },
      }),
    ).toEqual({ tokens: 12, finishReason: 'completed' });
  });

  it('reads Anthropic text and thinking deltas, stop reason and usage', () => {
    expect(
      parseStreamEvent('messages', {
        type: 'content_block_delta',
        delta: { type: 'text_delta', text: 'Hi' },
      }),
    ).toEqual({ content: 'Hi' });
    expect(
      parseStreamEvent('messages', {
        type: 'content_block_delta',
        delta: { type: 'thinking_delta', thinking: 'hmm' },
      }),
    ).toEqual({ reasoning: 'hmm' });
    expect(
      parseStreamEvent('messages', {
        type: 'message_delta',
        delta: { stop_reason: 'end_turn' },
        usage: { output_tokens: 9 },
      }),
    ).toEqual({ tokens: 9, finishReason: 'end_turn' });
  });

  it('surfaces an error in every format', () => {
    expect(parseStreamEvent('chat', { error: { message: 'chat failed' } })?.error).toBe(
      'chat failed',
    );
    expect(parseStreamEvent('chat', { error: 'plain failure' })?.error).toBe('plain failure');
    expect(
      parseStreamEvent('responses', { type: 'error', message: 'responses failed' })?.error,
    ).toBe('responses failed');
    expect(
      parseStreamEvent('responses', {
        type: 'response.failed',
        response: { error: { message: 'failed response' } },
      })?.error,
    ).toBe('failed response');
    expect(
      parseStreamEvent('messages', {
        type: 'error',
        error: { type: 'overloaded_error', message: 'messages failed' },
      })?.error,
    ).toBe('messages failed');
  });

  it('ignores payloads it cannot use', () => {
    expect(parseStreamEvent('responses', { type: 'response.created' })).toBeNull();
    expect(parseStreamEvent('chat', { choices: [] })).toBeNull();
    expect(parseStreamEvent('messages', null)).toBeNull();
  });
});

describe('buildCode', () => {
  it('shows a chat completion call with a bearer key', () => {
    const code = buildCode('chat', 'https://gw.example/', OPTIONS);
    expect(code).toContain('curl https://gw.example/v1/chat/completions \\');
    expect(code).toContain('Authorization: Bearer $HYBRID_API_KEY');
    expect(code).toContain('"model": "glm-4.6"');
  });

  it('shows a Responses call with stream and max_output_tokens', () => {
    const code = buildCode('responses', 'https://gw.example', OPTIONS);
    expect(code).toContain('/v1/responses');
    expect(code).toContain('"max_output_tokens": 512');
    expect(code).toContain('"stream": true');
  });

  it('shows an Anthropic Messages call with its headers', () => {
    const code = buildCode('messages', 'https://gw.example', OPTIONS);
    expect(code).toContain('/v1/messages');
    expect(code).toContain('x-api-key: $HYBRID_API_KEY');
    expect(code).toContain('anthropic-version: 2023-06-01');
  });

  it('escapes a quote in the prompt so the shell command stays valid', () => {
    const code = buildCode('chat', 'https://gw.example', {
      ...OPTIONS,
      history: [{ role: 'user', content: "it's fine" }],
    });
    expect(code).toContain("it'\\''s fine");
  });
});
