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
  it('posts the password to the key’s own reveal endpoint', async () => {
    fetchMock.mockResolvedValueOnce(json({ key_prefix: 'hyi-abc/123', api_key: 'hyi-full' }));

    await expect(revealApiKey('hyi-abc/123', 'my-password')).resolves.toEqual({
      key_prefix: 'hyi-abc/123',
      api_key: 'hyi-full',
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/user\/api-keys\/hyi-abc%2F123\/reveal$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ password: 'my-password' });
    expect(new Headers(init.headers).get('Authorization')).toBe(`Bearer ${TOKEN}`);
  });

  it('rejects with the gateway’s reason for a wrong password', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Current password is incorrect.' }, 400));

    await expect(revealApiKey('hyi-abc', 'wrong')).rejects.toThrow(
      'Current password is incorrect.',
    );
  });
});
