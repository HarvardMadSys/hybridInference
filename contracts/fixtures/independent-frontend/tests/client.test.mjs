import assert from 'node:assert/strict';
import test from 'node:test';

import { ControlError, createControlClient } from '../src/client.mjs';

test('uses stable errors and preserves request IDs', async () => {
  const client = createControlClient({
    fetchImpl: async () =>
      new Response(
        JSON.stringify({
          error: { code: 'EMAIL_NOT_VERIFIED', message: 'Verify first', details: {} },
          request_id: 'req-1',
        }),
        { status: 403, headers: { 'Content-Type': 'application/json' } },
      ),
  });

  await assert.rejects(
    client.login('person@example.test', 'secret'),
    (error) =>
      error instanceof ControlError &&
      error.code === 'EMAIL_NOT_VERIFIED' &&
      error.requestId === 'req-1',
  );
});

test('consumes login, capability, model, API-key, and streaming contracts', async () => {
  const calls = [];
  const responses = [
    new Response(JSON.stringify({ access_token: 'token', user: { id: 'u1' } }), { status: 200 }),
    new Response(
      JSON.stringify({
        schema_version: 1,
        control_api_version: '1.0',
        capabilities: { 'user.api_keys': true },
      }),
      { status: 200 },
    ),
    new Response(JSON.stringify({ id: 'u1', role: 'free' }), { status: 200 }),
    new Response(JSON.stringify({ object: 'list', data: [{ id: 'model-1' }] }), { status: 200 }),
    new Response(JSON.stringify({ api_key: 'sk-test', key_prefix: 'sk-test' }), { status: 201 }),
    new Response('data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n', {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }),
  ];
  const client = createControlClient({
    baseUrl: 'https://gateway.example.test/',
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return responses.shift();
    },
  });

  await client.login('person@example.test', 'secret');
  assert.equal((await client.capabilities()).schema_version, 1);
  assert.equal((await client.session()).id, 'u1');
  assert.equal((await client.models()).data[0].id, 'model-1');
  assert.equal((await client.createApiKey()).key_prefix, 'sk-test');
  const chunks = [];
  for await (const chunk of client.streamChat({
    model: 'model-1',
    messages: [{ role: 'user', content: 'hello' }],
  })) {
    chunks.push(chunk);
  }

  assert.equal(chunks.length, 1);
  assert.deepEqual(
    calls.map(({ url }) => url),
    [
      'https://gateway.example.test/auth/login',
      'https://gateway.example.test/capabilities',
      'https://gateway.example.test/user/me',
      'https://gateway.example.test/user/models',
      'https://gateway.example.test/user/api-keys',
      'https://gateway.example.test/v1/chat/completions',
    ],
  );
  assert.equal(new Headers(calls[1].init.headers).get('Authorization'), 'Bearer token');
  assert.equal(new Headers(calls[5].init.headers).get('Authorization'), 'Bearer sk-test');
});

test('parses CRLF and CR events across arbitrary byte and UTF-8 chunk boundaries', async () => {
  const encoder = new TextEncoder();
  const streamBytes = encoder.encode(
    'data: {"choices":[{"delta":{"content":"你好🌊"}}]}\r\n' +
      '\r\n' +
      'data: {"choices":[{"delta":{"content":"!"}}]}\r' +
      '\r' +
      'data: [DONE]\r\n' +
      '\r\n',
  );
  const responses = [
    new Response(JSON.stringify({ api_key: 'sk-test', key_prefix: 'sk-test' }), {
      status: 201,
    }),
    new Response(
      new ReadableStream({
        start(controller) {
          for (const byte of streamBytes) controller.enqueue(Uint8Array.of(byte));
          controller.close();
        },
      }),
      {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
      },
    ),
  ];
  const client = createControlClient({
    fetchImpl: async () => responses.shift(),
  });

  await client.createApiKey();
  const content = [];
  for await (const chunk of client.streamChat({ model: 'model-1', messages: [] })) {
    content.push(chunk.choices[0].delta.content);
  }

  assert.deepEqual(content, ['你好🌊', '!']);
});

test('rejects a stream that ends without the [DONE] frame', async () => {
  const responses = [
    new Response(JSON.stringify({ api_key: 'sk-test', key_prefix: 'sk-test' }), {
      status: 201,
    }),
    new Response('data: {"choices":[]}\n\n', {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }),
  ];
  const client = createControlClient({
    fetchImpl: async () => responses.shift(),
  });

  await client.createApiKey();
  await assert.rejects(
    async () => {
      for await (const _chunk of client.streamChat({ model: 'model-1', messages: [] })) {
        // Consume the stream so termination validation runs.
      }
    },
    (error) => error instanceof ControlError && error.code === 'INVALID_SSE_TERMINATION',
  );
});
