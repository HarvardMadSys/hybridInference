// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Client = typeof import('../client');

const API = 'https://console.example';

function token(sub: string): string {
  const encode = (value: object) =>
    btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  const exp = Math.floor(Date.now() / 1000) + 900;
  return `${encode({ alg: 'HS256' })}.${encode({ sub, exp })}.signature`;
}

function json(status: number, body: object): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** The gateway's refusal of a session that owes a code. */
function codeOwed(reason = 'unverified'): Response {
  return json(403, {
    error_code: 'TWO_FACTOR_REQUIRED',
    message: 'This needs a code from your authenticator app.',
    detail: 'This needs a code from your authenticator app.',
    reason,
  });
}

let client: Client;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  // The client keeps its token and its challenger in module state.
  vi.resetModules();
  client = await import('../client');
  sessionStorage.clear();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  client.setAccessToken(token('admin'));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('a request refused for want of an authenticator code', () => {
  it('is sent again, body and all, once the person gives a code', async () => {
    const challenger = vi.fn().mockResolvedValue(true);
    client.setTwoFactorChallenger(challenger);
    fetchMock.mockResolvedValueOnce(codeOwed('stale')).mockResolvedValueOnce(json(200, { ok: 1 }));

    const resp = await client.fetchWithAuth(API, '/admin/users/u1', {
      method: 'PATCH',
      body: '{"role":"admin"}',
    });

    expect(resp.status).toBe(200);
    expect(challenger).toHaveBeenCalledExactlyOnceWith({ reason: 'stale' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe(`${API}/admin/users/u1`);
    expect(fetchMock.mock.calls[1][1].method).toBe('PATCH');
    expect(fetchMock.mock.calls[1][1].body).toBe('{"role":"admin"}');
  });

  it('is handed back as it came when the person gives none', async () => {
    client.setTwoFactorChallenger(vi.fn().mockResolvedValue(false));
    fetchMock.mockResolvedValueOnce(codeOwed());

    const resp = await client.fetchWithAuth(API, '/admin/users');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    await expect(client.jsonOrThrow(resp)).rejects.toMatchObject({
      code: 'TWO_FACTOR_REQUIRED',
      message: 'This needs a code from your authenticator app.',
      statusCode: 403,
    });
  });

  it('is asked about once: a second refusal is the answer', async () => {
    const challenger = vi.fn().mockResolvedValue(true);
    client.setTwoFactorChallenger(challenger);
    fetchMock.mockResolvedValueOnce(codeOwed()).mockResolvedValueOnce(codeOwed());

    const resp = await client.fetchWithAuth(API, '/admin/users');

    expect(resp.status).toBe(403);
    expect(challenger).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('is not sent again as another account that signed in meanwhile', async () => {
    client.setTwoFactorChallenger(async () => {
      client.setAccessToken(token('someone-else'));
      return true;
    });
    fetchMock.mockResolvedValueOnce(codeOwed());

    await expect(client.fetchWithAuth(API, '/admin/users')).rejects.toMatchObject({
      code: 'ACCOUNT_CHANGED',
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('reaches the caller untouched when nothing is there to ask', async () => {
    fetchMock.mockResolvedValueOnce(codeOwed());

    const resp = await client.fetchWithAuth(API, '/admin/users');

    expect(resp.status).toBe(403);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('what is not a question for a code', () => {
  it.each([
    ['another 403', () => json(403, { detail: 'Admin access required' })],
    [
      'an account with no authenticator',
      () => json(403, { error_code: 'TWO_FACTOR_ENROLLMENT_REQUIRED', detail: 'Add one first.' }),
    ],
    ['a 403 that is not JSON', () => new Response('<html>', { status: 403 })],
  ])('%s', async (_name, refusal) => {
    const challenger = vi.fn().mockResolvedValue(true);
    client.setTwoFactorChallenger(challenger);
    fetchMock.mockResolvedValueOnce(refusal());

    const resp = await client.fetchWithAuth(API, '/admin/users');

    expect(resp.status).toBe(403);
    expect(challenger).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a refusal of the code itself, which would ask in a loop', async () => {
    const challenger = vi.fn().mockResolvedValue(true);
    client.setTwoFactorChallenger(challenger);
    fetchMock.mockResolvedValueOnce(codeOwed());

    const resp = await client.fetchWithAuth(API, '/auth/two-factor/verify', { method: 'POST' });

    expect(resp.status).toBe(403);
    expect(challenger).not.toHaveBeenCalled();
  });

  it('names the enrollment refusal by its code, for the page that sends people to Settings', async () => {
    const refusal = json(403, {
      error_code: 'TWO_FACTOR_ENROLLMENT_REQUIRED',
      message: 'Add an authenticator app in Settings first.',
      detail: 'Add an authenticator app in Settings first.',
    });

    await expect(client.jsonOrThrow(refusal)).rejects.toMatchObject({
      code: 'TWO_FACTOR_ENROLLMENT_REQUIRED',
      message: 'Add an authenticator app in Settings first.',
    });
  });
});

describe('refusals of the two-factor routes', () => {
  async function refusalOf(
    call: (api: typeof import('../twoFactor')) => Promise<unknown>,
    resp: Response,
  ) {
    fetchMock.mockResolvedValueOnce(resp);
    const twoFactor = await import('../twoFactor');
    return call(twoFactor).then(
      () => null,
      (error: unknown) => error,
    );
  }
  const typed = (status: number, message: string) =>
    json(status, { error: { type: 'unknown', message, code: status } });

  it('keep the gateway’s sentence for a conflict, not the signup one', async () => {
    const { getErrorMessage } = await import('@/lib/utils/errors');
    const error = await refusalOf(
      (api) => api.confirmTwoFactor('123456'),
      typed(409, 'This setup was replaced by a newer one. Start again.'),
    );
    expect(getErrorMessage(error)).toBe('This setup was replaced by a newer one. Start again.');
  });

  it('say that a recovery code still works while codes are held off', async () => {
    const { getErrorMessage } = await import('@/lib/utils/errors');
    const error = await refusalOf(
      (api) => api.verifyTwoFactor('000000'),
      typed(429, 'Too many wrong codes. Try again later, or use a recovery code.'),
    );
    expect(getErrorMessage(error)).toBe(
      'Too many wrong codes. Try again later, or use a recovery code.',
    );
  });
});
