import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setAccessToken } from '../client';
import {
  checkKeyAuthorization,
  createKeyAuthorizationCode,
  keyAuthorizationErrorType,
  type KeyAuthorizationRequest,
} from '../keyAuthorization';
import { APIError } from '@/lib/utils/errors';

const fetchMock = vi.fn();

const REQUEST: KeyAuthorizationRequest = {
  callback_url: 'https://agent.example.org/callback?state=abc',
  code_challenge: 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
  code_challenge_method: 'S256',
};

function token(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `header.${btoa(JSON.stringify({ sub, exp }))}.signature`;
}

function respond(data: unknown, status = 200) {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

function sent(call = 0): { url: string; options: RequestInit; body: unknown } {
  const [url, options] = fetchMock.mock.calls[call];
  return {
    url: String(url),
    options,
    body: typeof options?.body === 'string' ? JSON.parse(options.body) : undefined,
  };
}

function authorization(call = 0): string | null {
  return new Headers(sent(call).options.headers).get('Authorization');
}

let accessToken: string;

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('sessionStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  accessToken = token('owner');
  setAccessToken(accessToken);
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('checking a request before anyone signs in', () => {
  it('asks the gateway without a session', async () => {
    respond({
      app: 'agent.example.org',
      cancel_url: `${REQUEST.callback_url}&error=access_denied`,
    });

    await expect(checkKeyAuthorization(REQUEST)).resolves.toEqual({
      app: 'agent.example.org',
      cancel_url: `${REQUEST.callback_url}&error=access_denied`,
    });

    const request = sent();
    expect(request.url).toMatch(/\/v1\/auth\/keys\/check$/);
    expect(request.options.method).toBe('POST');
    expect(request.options.credentials).toBe('omit');
    // In the body, so the callback's state reaches no access log.
    expect(request.body).toEqual(REQUEST);
    expect(authorization()).toBeNull();
  });

  it.each([
    [400, 'invalid_request'],
    [404, 'not_configured'],
  ])('keeps the status and type of a %i refusal', async (status, type) => {
    respond({ error: { type, message: 'Refused.' } }, status);

    const error = await checkKeyAuthorization(REQUEST).catch((err: unknown) => err);

    expect(error).toBeInstanceOf(APIError);
    expect((error as APIError).statusCode).toBe(status);
    expect(keyAuthorizationErrorType(error)).toBe(type);
  });

  it('names no type for an answer that is not the gateway’s', async () => {
    fetchMock.mockResolvedValueOnce(new Response('<html>Bad gateway</html>', { status: 502 }));

    const error = await checkKeyAuthorization(REQUEST).catch((err: unknown) => err);

    expect((error as APIError).statusCode).toBe(502);
    expect(keyAuthorizationErrorType(error)).toBeNull();
  });
});

describe('issuing the application its code', () => {
  it('asks as this tab’s account', async () => {
    respond({
      code: 'code-1',
      redirect_url: `${REQUEST.callback_url}&code=code-1`,
      expires_in: 180,
      app: 'agent.example.org',
    });

    await expect(createKeyAuthorizationCode(REQUEST)).resolves.toMatchObject({
      code: 'code-1',
      redirect_url: `${REQUEST.callback_url}&code=code-1`,
    });

    const request = sent();
    expect(request.url).toMatch(/\/v1\/auth\/keys\/code$/);
    expect(request.options.method).toBe('POST');
    expect(request.body).toEqual(REQUEST);
    expect(authorization()).toBe(`Bearer ${accessToken}`);
  });

  it('asks as another account when given its token', async () => {
    respond({ code: 'code-2', redirect_url: 'https://agent.example.org/cb?code=code-2' });

    await createKeyAuthorizationCode(REQUEST, 'token-for-user_2');

    expect(authorization()).toBe('Bearer token-for-user_2');
  });

  it('keeps the type of the key ceiling', async () => {
    respond({ error: { type: 'key_limit_reached', message: 'Too many keys.' } }, 409);

    const error = await createKeyAuthorizationCode(REQUEST).catch((err: unknown) => err);

    expect((error as APIError).statusCode).toBe(409);
    expect(keyAuthorizationErrorType(error)).toBe('key_limit_reached');
  });

  it('reads an unverified address as the console does elsewhere', async () => {
    respond(
      { error: { type: 'email_not_verified', message: 'Verify your email address to continue.' } },
      403,
    );

    const error = await createKeyAuthorizationCode(REQUEST).catch((err: unknown) => err);

    expect(error).toMatchObject({ code: 'EMAIL_NOT_VERIFIED', statusCode: 403 });
    expect(keyAuthorizationErrorType(error)).toBe('email_not_verified');
  });
});
