import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { login, resendVerification } from '../auth';
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
});
