import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../client';
import { revealApiKey } from '../user';

const fetchMock = vi.fn();
const storage = new Map<string, string>();

// An unexpired token, so the request goes out without a refresh first.
const TOKEN = `header.${btoa(JSON.stringify({ sub: 'u1', exp: Math.floor(Date.now() / 1000) + 3600 }))}.signature`;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  storage.clear();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  setAccessToken(TOKEN);
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('revealApiKey', () => {
  it('posts to the key’s own reveal endpoint, with no password', async () => {
    fetchMock.mockResolvedValueOnce(json({ key_prefix: 'hyi-abc/123', api_key: 'hyi-full' }));

    await expect(revealApiKey('hyi-abc/123')).resolves.toEqual({
      key_prefix: 'hyi-abc/123',
      api_key: 'hyi-full',
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/user\/api-keys\/hyi-abc%2F123\/reveal$/);
    expect(init.method).toBe('POST');
    expect(init.body).toBeUndefined();
    expect(new Headers(init.headers).get('Authorization')).toBe(`Bearer ${TOKEN}`);
  });

  it('rejects with the gateway’s reason', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'API key not found' }, 404));

    await expect(revealApiKey('hyi-abc')).rejects.toThrow('API key not found');
  });
});
