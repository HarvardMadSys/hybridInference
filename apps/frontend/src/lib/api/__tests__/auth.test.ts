import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { endAgentSession, login, resendVerification } from '../auth';
import { APIError } from '@/lib/utils/errors';

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('sessionStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('resendVerification', () => {
  it('POSTs the email to /auth/resend-verification', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ message: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await resendVerification('user@example.com');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/auth\/resend-verification$/);
    expect(init).toMatchObject({ method: 'POST' });
    expect(JSON.parse(init.body as string)).toEqual({ email: 'user@example.com' });
  });
});

describe('login error mapping', () => {
  it('maps a plain 403 "Email not verified" detail to EMAIL_NOT_VERIFIED', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          detail: 'Email not verified. Please check your email for the verification link.',
        }),
        { status: 403, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    await expect(login({ email: 'user@example.com', password: 'pw' })).rejects.toMatchObject({
      code: 'EMAIL_NOT_VERIFIED',
    });
  });

  it('throws an APIError instance so callers can branch on the code', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Email not verified.' }), {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const err = await login({ email: 'user@example.com', password: 'pw' }).catch((e) => e);
    expect(err).toBeInstanceOf(APIError);
    expect((err as APIError).code).toBe('EMAIL_NOT_VERIFIED');
  });

  it('does not map unrelated "not verified" details to EMAIL_NOT_VERIFIED', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Signature not verified.' }), {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const err = await login({ email: 'user@example.com', password: 'pw' }).catch((e) => e);
    expect(err).toBeInstanceOf(APIError);
    expect((err as APIError).code).toBe('UNKNOWN_ERROR');
  });
});

describe('endAgentSession', () => {
  it('POSTs the agent logout endpoint under the same-origin agents proxy', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 200 }));

    await endAgentSession('/agents');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/agents/api/v1/session/logout');
    expect(init).toMatchObject({ method: 'POST', mode: 'no-cors', credentials: 'include' });
  });

  it('POSTs the agent logout endpoint on an agent served from its own host', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 200 }));

    await endAgentSession('https://agents.example.test/');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://agents.example.test/api/v1/session/logout');
    expect(init).toMatchObject({ method: 'POST', mode: 'no-cors', credentials: 'include' });
  });

  it('does nothing when no agent is deployed', async () => {
    await endAgentSession('');

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('never throws, so a sign-out is not blocked by an unreachable agent', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await expect(endAgentSession('https://agents.example.test')).resolves.toBeUndefined();
  });
});
