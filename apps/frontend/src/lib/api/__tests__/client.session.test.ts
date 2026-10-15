// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Client = typeof import('../client');

const API = 'https://console.example';

function token(sub: string, expiresInSeconds: number): string {
  const encode = (value: object) =>
    btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  const exp = Math.floor(Date.now() / 1000) + expiresInSeconds;
  return `${encode({ alg: 'HS256' })}.${encode({ sub, exp })}.signature`;
}

function refreshAnswer(accessToken: string): Response {
  return new Response(JSON.stringify({ access_token: accessToken }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

let client: Client;
let fetchMock: ReturnType<typeof vi.fn>;
let accountChangedEvents: number;
let authExpiredEvents: number;

function onAccountChanged() {
  accountChangedEvents += 1;
}
function onAuthExpired() {
  authExpiredEvents += 1;
}

beforeEach(async () => {
  // The client keeps its token and its account-change block in module state.
  vi.resetModules();
  client = await import('../client');
  sessionStorage.clear();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  accountChangedEvents = 0;
  authExpiredEvents = 0;
  window.addEventListener(client.ACCOUNT_CHANGED_EVENT, onAccountChanged);
  window.addEventListener(client.AUTH_EXPIRED_EVENT, onAuthExpired);
});

afterEach(() => {
  window.removeEventListener(client.ACCOUNT_CHANGED_EVENT, onAccountChanged);
  window.removeEventListener(client.AUTH_EXPIRED_EVENT, onAuthExpired);
  vi.unstubAllGlobals();
});

function sentAuthorization(call: number): string | null {
  return new Headers(fetchMock.mock.calls[call][1].headers).get('Authorization');
}

describe('refresh after a sign-in in another tab', () => {
  it('installs the refreshed token when it names the account this tab shows', async () => {
    const renewed = token('user-a', 900);
    client.setAccessToken(token('user-a', -60));
    fetchMock.mockResolvedValueOnce(refreshAnswer(renewed));
    fetchMock.mockResolvedValueOnce(new Response('{}'));

    await client.fetchWithAuth(API, '/user/me');

    expect(String(fetchMock.mock.calls[0][0])).toBe(`${API}/auth/refresh`);
    expect(sentAuthorization(1)).toBe(`Bearer ${renewed}`);
    expect(client.getAccessToken()).toBe(renewed);
    expect(accountChangedEvents).toBe(0);
  });

  it('refuses a token for another account and sends nothing as either', async () => {
    client.setAccessToken(token('user-a', -60));
    fetchMock.mockResolvedValueOnce(refreshAnswer(token('user-b', 900)));

    await expect(client.fetchWithAuth(API, '/user/api-keys')).rejects.toMatchObject({
      code: 'ACCOUNT_CHANGED',
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(accountChangedEvents).toBe(1);
    expect(authExpiredEvents).toBe(0);
    // Dropped, so a reload starts from the account the cookie now holds.
    expect(client.getAccessToken()).toBeNull();
    expect(sessionStorage.getItem('access_token')).toBeNull();

    await expect(client.fetchWithAuth(API, '/user/usage')).rejects.toMatchObject({
      code: 'ACCOUNT_CHANGED',
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('refuses it on the retry after a 401 too, rather than reporting an expired session', async () => {
    client.setAccessToken(token('user-a', 900));
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 401 }));
    fetchMock.mockResolvedValueOnce(refreshAnswer(token('user-b', 900)));

    await expect(client.fetchWithAuth(API, '/user/me')).rejects.toMatchObject({
      code: 'ACCOUNT_CHANGED',
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(accountChangedEvents).toBe(1);
    expect(authExpiredEvents).toBe(0);
  });

  it('lets a sign-in in this tab lift the block', async () => {
    client.setAccessToken(token('user-a', -60));
    fetchMock.mockResolvedValueOnce(refreshAnswer(token('user-b', 900)));
    await expect(client.fetchWithAuth(API, '/user/me')).rejects.toMatchObject({
      code: 'ACCOUNT_CHANGED',
    });

    const signedIn = token('user-b', 900);
    client.setAccessToken(signedIn);
    fetchMock.mockResolvedValueOnce(new Response('{}'));

    await client.fetchWithAuth(API, '/user/me');

    expect(sentAuthorization(1)).toBe(`Bearer ${signedIn}`);
  });

  it('accepts whichever account the cookie holds when the tab shows none yet', async () => {
    const issued = token('user-b', 900);
    fetchMock.mockResolvedValueOnce(refreshAnswer(issued));
    fetchMock.mockResolvedValueOnce(new Response('{}'));

    await client.fetchWithAuth(API, '/user/me');

    expect(client.getAccessToken()).toBe(issued);
    expect(accountChangedEvents).toBe(0);
  });
});

describe('refresh across tabs', () => {
  it('holds the origin-wide refresh lock while the refresh is in flight', async () => {
    const events: string[] = [];
    const request = vi.fn(async (name: string, run: () => Promise<unknown>) => {
      events.push(`lock ${name}`);
      const result = await run();
      events.push('unlock');
      return result;
    });
    vi.stubGlobal('navigator', { locks: { request } });
    fetchMock.mockImplementationOnce(async () => {
      events.push('refresh');
      return refreshAnswer(token('user-a', 900));
    });
    fetchMock.mockResolvedValueOnce(new Response('{}'));

    await client.fetchWithAuth(API, '/user/me');

    expect(events).toEqual(['lock hybridinference:auth-refresh', 'refresh', 'unlock']);
  });

  it('still refreshes where Web Locks are unavailable', async () => {
    vi.stubGlobal('navigator', {});
    fetchMock.mockResolvedValueOnce(refreshAnswer(token('user-a', 900)));
    fetchMock.mockResolvedValueOnce(new Response('{}'));

    await client.fetchWithAuth(API, '/user/me');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
