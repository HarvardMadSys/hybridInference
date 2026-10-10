import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAccessToken, setAccessToken } from '../client';
import {
  createSetupAdmin,
  getSetupStatus,
  isWellFormedSetupCode,
  normalizeSetupCode,
} from '../setup';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

const fetchMock = vi.fn();
const storage = new Map<string, string>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const LOGIN_RESPONSE = {
  access_token: 'header.payload.signature',
  token_type: 'bearer',
  expires_in: 900,
  user: { id: 'u1', email: null, login_name: 'admin', role: 'admin', is_admin: true },
};

beforeEach(() => {
  fetchMock.mockReset();
  storage.clear();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('getSetupStatus', () => {
  it('reads the public status endpoint', async () => {
    fetchMock.mockResolvedValueOnce(json({ setup_required: true, database_enabled: true }));

    await expect(getSetupStatus()).resolves.toEqual({
      setup_required: true,
      database_enabled: true,
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/auth\/setup\/status$/);
    expect(init).toMatchObject({ method: 'GET', cache: 'no-store' });
  });

  it('treats a gateway without the endpoint as nothing to set up', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Not Found' }, 404));

    await expect(getSetupStatus()).resolves.toEqual({
      setup_required: false,
      database_enabled: false,
    });
  });

  it('surfaces any other failure', async () => {
    fetchMock.mockResolvedValueOnce(new Response('<html>bad gateway</html>', { status: 502 }));

    await expect(getSetupStatus()).rejects.toBeInstanceOf(APIError);
  });
});

describe('createSetupAdmin', () => {
  it('posts the request and keeps the session like a login', async () => {
    fetchMock.mockResolvedValueOnce(json(LOGIN_RESPONSE));

    const result = await createSetupAdmin({
      setup_code: 'ABCD-EFGH-JKLM',
      login_name: 'admin',
      password: 'Secret123',
      display_name: 'Ops',
    });

    expect(result.user.login_name).toBe('admin');
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/auth\/setup\/admin$/);
    expect(init).toMatchObject({ method: 'POST', credentials: 'include' });
    expect(JSON.parse(init.body as string)).toEqual({
      setup_code: 'ABCD-EFGH-JKLM',
      login_name: 'admin',
      password: 'Secret123',
      display_name: 'Ops',
    });
    expect(getAccessToken()).toBe('header.payload.signature');
  });

  it('leaves an empty display name out of the request', async () => {
    fetchMock.mockResolvedValueOnce(json(LOGIN_RESPONSE));

    await createSetupAdmin({
      setup_code: 'ABCD-EFGH-JKLM',
      login_name: 'admin',
      password: 'Secret123',
      display_name: '',
    });

    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).not.toHaveProperty(
      'display_name',
    );
  });

  it.each([
    [403, { detail: 'Invalid setup code' }],
    [409, { detail: 'Setup is already complete' }],
    [503, { detail: 'Database unavailable' }],
  ])('keeps the %i status and the server message', async (status, body) => {
    fetchMock.mockResolvedValueOnce(json(body, status));

    const error = await createSetupAdmin({
      setup_code: 'ABCD-EFGH-JKLM',
      login_name: 'admin',
      password: 'Secret123',
    }).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(APIError);
    expect((error as APIError).statusCode).toBe(status);
    // Verbatim: not reclassified by keyword ("expired", "token"…) into a
    // login error the page would then mis-explain.
    expect(getErrorMessage(error)).toBe(body.detail);
    expect(getAccessToken()).toBeNull();
  });

  it('maps a validation error to the request fields', async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          detail: [
            {
              loc: ['body', 'login_name'],
              msg: 'String should match pattern',
              type: 'string_pattern_mismatch',
            },
            {
              loc: ['body', 'password'],
              msg: 'Value error, Password must contain at least one number',
              type: 'value_error',
            },
          ],
        },
        422,
      ),
    );

    const error = (await createSetupAdmin({
      setup_code: 'ABCD-EFGH-JKLM',
      login_name: '-x',
      password: 'Secret',
    }).catch((e: unknown) => e)) as APIError;

    expect(error.statusCode).toBe(422);
    expect(error.details?.fields).toEqual({
      login_name: 'String should match pattern',
      password: 'Password must contain at least one number',
    });
  });

  it('reports a rate limit as such', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Too many setup attempts' }, 429));

    const error = (await createSetupAdmin({
      setup_code: 'ABCD-EFGH-JKLM',
      login_name: 'admin',
      password: 'Secret123',
    }).catch((e: unknown) => e)) as APIError;

    expect(error.statusCode).toBe(429);
    expect(error.code).toBe('RATE_LIMIT_EXCEEDED');
  });
});

describe('setup code input', () => {
  it.each([
    ['abcd-efgh-jklm', 'ABCD-EFGH-JKLM'],
    ['ABCDEFGHJKLM', 'ABCD-EFGH-JKLM'],
    ['  abcd efgh jklm ', 'ABCD-EFGH-JKLM'],
    ['ABCD--EFGH-JKLM', 'ABCD-EFGH-JKLM'],
  ])('normalizes %j to the logged form', (raw, expected) => {
    expect(normalizeSetupCode(raw)).toBe(expected);
    expect(isWellFormedSetupCode(raw)).toBe(true);
  });

  it('leaves a code of the wrong length for validation to report', () => {
    expect(normalizeSetupCode(' abc-def ')).toBe('ABC-DEF');
    expect(isWellFormedSetupCode('ABC-DEF')).toBe(false);
  });
});
