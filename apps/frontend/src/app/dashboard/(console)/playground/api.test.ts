import { describe, expect, it } from 'vitest';

import {
  CODE_LANGUAGES,
  buildCode,
  buildRequest,
  endpointFor,
  parseStreamEvent,
  type RequestOptions,
} from './api';

const OPTIONS: RequestOptions = {
  model: 'glm-4.6',
  history: [
    { role: 'user', content: 'hi' },
    { role: 'assistant', content: 'hello' },
  ],
  systemPrompt: 'Be terse.',
  temperature: 0.5,
  maxTokens: 512,
  reasoningEffort: null,
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
      store: false,
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

  it('sends the reasoning effort in each format shape', () => {
    const options = { ...OPTIONS, reasoningEffort: 'high' };
    expect(buildRequest('chat', options)).toMatchObject({ reasoning_effort: 'high' });
    expect(buildRequest('responses', options)).toMatchObject({
      reasoning: { effort: 'high' },
    });
    expect(buildRequest('messages', options)).toMatchObject({
      output_config: { effort: 'high' },
    });
  });

  it('omits the reasoning effort when none is chosen', () => {
    expect(buildRequest('chat', OPTIONS)).not.toHaveProperty('reasoning_effort');
    expect(buildRequest('responses', OPTIONS)).not.toHaveProperty('reasoning');
    expect(buildRequest('messages', OPTIONS)).not.toHaveProperty('output_config');
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
  it('offers Python before cURL', () => {
    expect(CODE_LANGUAGES.map((option) => option.label)).toEqual(['Python', 'cURL']);
  });

  it('shows a chat completion call with a bearer key', () => {
    const code = buildCode('curl', 'chat', 'https://gw.example/', OPTIONS);
    expect(code).toContain('curl https://gw.example/v1/chat/completions \\');
    expect(code).toContain('Authorization: Bearer $HYBRID_API_KEY');
    expect(code).toContain('"model": "glm-4.6"');
  });

  it('sends the public Chat shape: system prompt in messages, streaming on', () => {
    const code = buildCode('curl', 'chat', 'https://gw.example', OPTIONS);
    const json = code.slice(code.indexOf("-d '") + 4, code.lastIndexOf("'"));
    const body = JSON.parse(json);
    expect(body).toEqual({
      model: 'glm-4.6',
      messages: [{ role: 'system', content: 'Be terse.' }, ...OPTIONS.history],
      temperature: 0.5,
      max_tokens: 512,
      stream: true,
    });
    expect(body).not.toHaveProperty('system_prompt');
    expect(body).not.toHaveProperty('provider');
  });

  it('keeps the chosen provider as an X-Route-Pin header on Chat only', () => {
    expect(buildCode('curl', 'chat', 'https://gw.example', OPTIONS)).toContain(
      "-H 'X-Route-Pin: glm-4.6:zai-api'",
    );
    expect(buildCode('python', 'chat', 'https://gw.example', OPTIONS)).toContain(
      'extra_headers={"X-Route-Pin": "glm-4.6:zai-api"}',
    );
    for (const api of ['responses', 'messages'] as const) {
      expect(buildCode('curl', api, 'https://gw.example', OPTIONS)).not.toContain('X-Route-Pin');
      expect(buildCode('python', api, 'https://gw.example', OPTIONS)).not.toContain('X-Route-Pin');
    }
    const unpinned = { ...OPTIONS, provider: null };
    expect(buildCode('curl', 'chat', 'https://gw.example', unpinned)).not.toContain('X-Route-Pin');
  });

  it('shows a Responses call with stream and max_output_tokens', () => {
    const code = buildCode('curl', 'responses', 'https://gw.example', OPTIONS);
    expect(code).toContain('/v1/responses');
    expect(code).toContain('"max_output_tokens": 512');
    expect(code).toContain('"stream": true');
  });

  it('shows an Anthropic Messages call with its headers', () => {
    const code = buildCode('curl', 'messages', 'https://gw.example', OPTIONS);
    expect(code).toContain('/v1/messages');
    expect(code).toContain('x-api-key: $HYBRID_API_KEY');
    expect(code).toContain('anthropic-version: 2023-06-01');
  });

  it('escapes a quote in the prompt so the shell command stays valid', () => {
    const code = buildCode('curl', 'chat', 'https://gw.example', {
      ...OPTIONS,
      history: [{ role: 'user', content: "it's fine" }],
    });
    expect(code).toContain("it'\\''s fine");
  });

  it('writes a runnable Python chat call against the gateway', () => {
    const code = buildCode('python', 'chat', 'https://gw.example/', { ...OPTIONS, provider: null });
    expect(code).toContain('from openai import OpenAI');
    expect(code).toContain('base_url="https://gw.example/v1"');
    expect(code).toContain('client.chat.completions.create(');
    expect(code).toContain('model="glm-4.6"');
    expect(code).toContain('{"role": "system", "content": "Be terse."},');
    expect(code).toContain('{"role": "user", "content": "hi"},');
    expect(code).toContain('max_tokens=512');
    expect(code).toContain('stream=True');
    expect(code).not.toContain('provider');

    const withEffort = buildCode('python', 'chat', 'https://gw.example', {
      ...OPTIONS,
      reasoningEffort: 'high',
    });
    expect(withEffort).toContain('reasoning_effort="high"');
  });

  it('writes a Python Responses call with instructions and input', () => {
    const code = buildCode('python', 'responses', 'https://gw.example', OPTIONS);
    expect(code).toContain('client.responses.create(');
    expect(code).toContain('instructions="Be terse."');
    expect(code).toContain('input=[');
    expect(code).toContain('max_output_tokens=512');
    expect(code).toContain('store=False');

    const withEffort = buildCode('python', 'responses', 'https://gw.example', {
      ...OPTIONS,
      reasoningEffort: 'low',
    });
    expect(withEffort).toContain('reasoning={"effort": "low"}');
  });

  it('writes a Python Anthropic call with its own SDK and base URL', () => {
    const code = buildCode('python', 'messages', 'https://gw.example', OPTIONS);
    expect(code).toContain('from anthropic import Anthropic');
    expect(code).toContain('base_url="https://gw.example"');
    expect(code).toContain('client.messages.create(');
    expect(code).toContain('system="Be terse."');
    expect(code).toContain('max_tokens=512');

    const withEffort = buildCode('python', 'messages', 'https://gw.example', {
      ...OPTIONS,
      reasoningEffort: 'medium',
    });
    expect(withEffort).toContain('output_config={"effort": "medium"}');
  });

  it('omits an empty system prompt from Python', () => {
    const options = { ...OPTIONS, systemPrompt: '  ' };
    expect(buildCode('python', 'chat', 'https://gw.example', options)).not.toContain('"system"');
    expect(buildCode('python', 'messages', 'https://gw.example', options)).not.toContain('system=');
  });
});
