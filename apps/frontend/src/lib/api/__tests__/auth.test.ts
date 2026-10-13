import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import {
  AGENT_LOGOUT_TIMEOUT_MS,
  endAgentSession,
  bindPhone,
  login,
  loginPhone,
  resendVerification,
  sendPhoneCode,
  signupPhone,
} from '../auth';
import { getAccessToken, setAccessToken } from '../client';
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
  setAccessToken(null);
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('phone auth requests', () => {
  function respond(data: unknown) {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(data), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  }

  function validToken() {
    return `header.${btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }))}.signature`;
  }

  it('sends the purpose and captcha token without a browser-side credential', async () => {
    respond({ message: 'sent', retry_after: 60 });
    await sendPhoneCode({ phone: '13812345678', purpose: 'signup', turnstileToken: 'captcha' });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/auth\/phone\/code$/);
    expect(options.credentials).toBe('include');
    expect(JSON.parse(options.body)).toEqual({
      phone: '13812345678',
      purpose: 'signup',
      turnstile_token: 'captcha',
    });
  });

  it('sends signup consent and accepts a phone-only identity', async () => {
    const result = {
      message: 'created',
      user_id: 'phone-user',
      email: null,
      phone: '+8613812345678',
      requires_approval: true,
    };
    respond(result);
    const request = {
      phone: '13812345678',
      code: '123456',
      user_name: 'Phone User',
      accepted_tos: true as const,
    };
    expect(await signupPhone(request)).toEqual(result);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/auth\/phone\/signup$/);
    expect(JSON.parse(options.body)).toEqual(request);
  });

  it('uses the shared access-token and HttpOnly refresh-cookie flow for phone login', async () => {
    const accessToken = validToken();
    respond({
      access_token: accessToken,
      token_type: 'bearer',
      expires_in: 3600,
      user: {
        id: 'phone-user',
        email: null,
        phone: '+8613812345678',
        phone_verified: true,
        role: 'free',
        is_admin: false,
      },
    });
    const result = await loginPhone({ phone: '13812345678', code: '123456' });
    expect(result.user.email).toBeNull();
    expect(getAccessToken()).toBe(accessToken);
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/auth\/phone\/login$/);
    expect(fetchMock.mock.calls[0][1].credentials).toBe('include');
  });

  it('authenticates both sending a bind code and binding an existing account', async () => {
    const accessToken = validToken();
    setAccessToken(accessToken);
    respond({ message: 'sent', retry_after: 60 });
    await sendPhoneCode({ phone: '13812345678', purpose: 'bind' });
    respond({ message: 'bound' });
    await bindPhone({ phone: '13812345678', code: '123456' });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringMatching(/\/auth\/phone\/code$/),
      expect.stringMatching(/\/auth\/phone\/bind$/),
    ]);
    for (const [, options] of fetchMock.mock.calls) {
      expect(options.headers.get('Authorization')).toBe(`Bearer ${accessToken}`);
      expect(options.credentials).toBe('include');
    }
  });
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

  it('gives up on an agent that never answers, so sign-in and sign-out still finish', async () => {
    const timeout = new AbortController();
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timeout.signal);
    try {
      // Pends until aborted, like an agent that accepts the connection and stalls.
      fetchMock.mockImplementationOnce(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () =>
              reject(new DOMException('Aborted', 'TimeoutError')),
            );
          }),
      );

      const pending = endAgentSession('https://agents.example.test');
      timeout.abort();

      await expect(pending).resolves.toBeUndefined();
      expect(timeoutSpy).toHaveBeenCalledWith(AGENT_LOGOUT_TIMEOUT_MS);
    } finally {
      timeoutSpy.mockRestore();
    }
  });
});
