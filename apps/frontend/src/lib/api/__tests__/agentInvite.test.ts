import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { redeemAgentInvite } from '../agentInvite';
import { getAccessToken, setAccessToken } from '../client';

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('sessionStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  setAccessToken(null);
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('redeemAgentInvite', () => {
  it('posts the token and password, stores the session token, returns the key once', async () => {
    const accessToken = `header.${btoa(
      JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }),
    )}.signature`;
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          access_token: accessToken,
          token_type: 'bearer',
          expires_in: 900,
          user: { id: 'u1', email: 'a@example.org', role: 'free', is_admin: false },
          api_key: 'hyi-scoped-key',
          account_created: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const result = await redeemAgentInvite({
      token: 'invitation-token',
      password: 'password123',
      acceptedTos: true,
    });

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/v1\/agent-invites\/redeem$/);
    expect(options.credentials).toBe('include');
    expect(JSON.parse(options.body)).toEqual({
      token: 'invitation-token',
      password: 'password123',
      accepted_tos: true,
    });
    expect(result.api_key).toBe('hyi-scoped-key');
    expect(result.account_created).toBe(true);
    expect(getAccessToken()).toBe(accessToken);
  });
});
