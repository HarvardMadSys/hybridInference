import { describe, expect, it, vi, afterEach } from 'vitest';

import { APIError } from '@/lib/utils/errors';
import { jsonOrThrow, safeFetch } from '../client';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('jsonOrThrow', () => {
  it('returns parsed JSON for ok responses', async () => {
    const resp = new Response(JSON.stringify({ hello: 'world' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
    await expect(jsonOrThrow<{ hello: string }>(resp)).resolves.toEqual({ hello: 'world' });
  });

  it('classifies a non-JSON 504 (edge timeout HTML page) as TIMEOUT_ERROR, not NETWORK_ERROR', async () => {
    const resp = new Response('<html>gateway timeout</html>', {
      status: 504,
      statusText: 'Gateway Timeout',
      headers: { 'Content-Type': 'text/html' },
    });
    await expect(jsonOrThrow(resp)).rejects.toMatchObject({
      code: 'TIMEOUT_ERROR',
      statusCode: 504,
    });
  });

  it('classifies a non-JSON 502 as SERVICE_UNAVAILABLE', async () => {
    const resp = new Response('<html>bad gateway</html>', {
      status: 502,
      statusText: 'Bad Gateway',
      headers: { 'Content-Type': 'text/html' },
    });
    await expect(jsonOrThrow(resp)).rejects.toMatchObject({ code: 'SERVICE_UNAVAILABLE' });
  });

  it('classifies a non-JSON 500 as SERVER_ERROR', async () => {
    const resp = new Response('oops', { status: 500, statusText: 'Internal Server Error' });
    await expect(jsonOrThrow(resp)).rejects.toMatchObject({ code: 'SERVER_ERROR' });
  });

  it('still maps structured backend JSON errors by content', async () => {
    const resp = new Response(JSON.stringify({ error: { message: 'token expired' } }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
    await expect(jsonOrThrow(resp)).rejects.toMatchObject({ code: 'TOKEN_EXPIRED' });
  });
});

describe('safeFetch', () => {
  it('rethrows a fetch TypeError (real connectivity failure) as NETWORK_ERROR', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')));
    await expect(safeFetch('https://example.test/x')).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
    });
  });

  it('passes through a received response unchanged', async () => {
    const resp = new Response('ok', { status: 200 });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(resp));
    await expect(safeFetch('https://example.test/x')).resolves.toBe(resp);
  });

  it('does not wrap an APIError that already has a code', async () => {
    const original = new APIError('TOKEN_EXPIRED', 'nope');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(original));
    await expect(safeFetch('https://example.test/x')).rejects.toBe(original);
  });
});
