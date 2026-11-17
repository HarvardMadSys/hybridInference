import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { resumeSession } from '../auth';
import { getAccessToken, setAccessToken } from '../client';
import {
  cancelSsoLink,
  confirmSsoLink,
  discoverSso,
  getPendingSsoLink,
  linkSsoIdentity,
  listSsoIdentities,
  ssoErrorCode,
  ssoReauthPurpose,
  ssoRefusalMessage,
  ssoStartUrl,
  startSsoReauth,
  unlinkSsoIdentity,
} from '../sso';
import { APIError } from '@/lib/utils/errors';

const fetchMock = vi.fn();

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

/** The gateway's error body. */
function refuse(status: number, message: string) {
  respond({ error: { message, type: 'invalid_request_error', code: status } }, status);
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

describe('starting a sign-in', () => {
  it('asks which provider answers for an email, without an account', async () => {
    respond({ available: true, authorization_url: 'https://idp.example.edu/authorize?state=s' });

    await expect(discoverSso('alice@example.edu', '/dashboard/keys')).resolves.toBe(
      'https://idp.example.edu/authorize?state=s',
    );

    const request = sent();
    expect(request.url).toMatch(/\/auth\/sso\/discover$/);
    expect(request.options.method).toBe('POST');
    // The binding cookie comes back on this response.
    expect(request.options.credentials).toBe('include');
    // In the body, so the address reaches no access log.
    expect(request.body).toEqual({ email: 'alice@example.edu', return_to: '/dashboard/keys' });
    expect(authorization()).toBeNull();
  });

  it('resolves to nothing when no provider answers for the email', async () => {
    respond({ available: false, authorization_url: null });

    await expect(discoverSso('alice@nowhere.test', '/dashboard')).resolves.toBeNull();
  });

  it.each([429, 503])('keeps the status of a %i for the page to explain', async (status) => {
    refuse(status, 'Single sign-on is unavailable right now.');

    await expect(discoverSso('alice@example.edu', '/dashboard')).rejects.toMatchObject({
      statusCode: status,
    });
  });

  // The address is handed to location.assign.
  it.each([null, '', 'javascript:alert(1)', '/login', 'data:text/html,x'])(
    'refuses an address that is not a web address: %j',
    async (url) => {
      respond({ available: true, authorization_url: url });

      await expect(discoverSso('alice@example.edu', '/dashboard')).rejects.toBeInstanceOf(APIError);
    },
  );

  it('builds the link that starts a sign-in at one known provider', () => {
    expect(ssoStartUrl('example-univ', '/authorize?client_id=agent&x=1')).toMatch(
      /\/auth\/sso\/example-univ\/start\?return_to=%2Fauthorize%3Fclient_id%3Dagent%26x%3D1$/,
    );
  });
});

describe('finishing a sign-in the gateway answered with a redirect', () => {
  it('takes the token from the session cookie and names the account', async () => {
    setAccessToken(null);
    const issued = token('sso-user');
    respond({ access_token: issued, token_type: 'bearer', expires_in: 900 });

    await expect(resumeSession()).resolves.toBe('sso-user');

    expect(sent().url).toMatch(/\/auth\/refresh$/);
    expect(getAccessToken()).toBe(issued);
  });

  it('throws when the browser holds no session', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 401 }));

    await expect(resumeSession()).rejects.toMatchObject({ code: 'SESSION_NOT_FOUND' });
    expect(getAccessToken()).toBeNull();
  });
});

describe('an identity waiting to be linked', () => {
  const waiting = {
    provider_id: 'example-univ',
    provider_name: 'Example University',
    email: 'alice@example.edu',
  };

  it('is read with the cookie alone', async () => {
    setAccessToken(null);
    respond(waiting);

    await expect(getPendingSsoLink()).resolves.toEqual(waiting);

    expect(sent().url).toMatch(/\/auth\/sso\/link$/);
    expect(sent().options.credentials).toBe('include');
    // One request: no token is asked for on the way.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('is nothing once it expired', async () => {
    refuse(404, 'No single sign-on is waiting to be linked.');

    await expect(getPendingSsoLink()).resolves.toBeNull();
  });

  it('still fails when the gateway does', async () => {
    refuse(500, 'Database not available');

    await expect(getPendingSsoLink()).rejects.toMatchObject({ statusCode: 500 });
  });

  it('is linked as the signed-in account', async () => {
    respond({ message: 'Single sign-on connected.' });

    await expect(confirmSsoLink()).resolves.toEqual({ message: 'Single sign-on connected.' });

    expect(sent().url).toMatch(/\/auth\/sso\/link\/confirm$/);
    expect(sent().options.method).toBe('POST');
    expect(authorization()).toBe(`Bearer ${accessToken}`);
  });

  it('keeps the gateway’s reason when it does not belong on this account', async () => {
    refuse(409, 'This single sign-on is already connected to another account.');

    const refusal = await confirmSsoLink().catch((error: unknown) => error);

    expect(refusal).toMatchObject({ statusCode: 409 });
    expect(ssoRefusalMessage(refusal)).toBe(
      'This single sign-on is already connected to another account.',
    );
  });

  it('is dropped without an account', async () => {
    setAccessToken(null);
    respond({ message: 'Single sign-on was not connected.' });

    await cancelSsoLink();

    expect(sent().url).toMatch(/\/auth\/sso\/link\/cancel$/);
    expect(sent().options).toMatchObject({ method: 'POST', credentials: 'include' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('confirming a sensitive action at the provider', () => {
  it('starts as the signed-in account, for one purpose and one page to return to', async () => {
    respond({
      available: true,
      authorization_url: 'https://idp.example.edu/authorize?prompt=login',
    });

    await expect(startSsoReauth('delete_account', '/dashboard/settings')).resolves.toBe(
      'https://idp.example.edu/authorize?prompt=login',
    );

    expect(sent().url).toMatch(/\/auth\/sso\/reauth\/start$/);
    expect(sent().body).toEqual({ purpose: 'delete_account', return_to: '/dashboard/settings' });
    expect(authorization()).toBe(`Bearer ${accessToken}`);
  });

  it('fails when the account has no identity to confirm with', async () => {
    refuse(409, 'This account has no single sign-on to confirm with.');

    await expect(startSsoReauth('set_password', '/dashboard/settings')).rejects.toMatchObject({
      statusCode: 409,
    });
  });

  it('fails rather than leaving the page with nowhere to go', async () => {
    respond({ available: false, authorization_url: null });

    await expect(startSsoReauth('bind_email', '/dashboard/settings')).rejects.toBeInstanceOf(
      APIError,
    );
  });
});

describe('the identities of the signed-in account', () => {
  const identity = {
    id: 'idn_1',
    provider_id: 'example-univ',
    provider_name: 'Example University',
    email: 'alice@example.edu',
    linked_at: '2026-11-01T09:00:00Z',
    last_login_at: null,
    available: true,
  };

  it('are listed', async () => {
    respond({ identities: [identity] });

    await expect(listSsoIdentities()).resolves.toEqual([identity]);

    expect(sent().url).toMatch(/\/auth\/sso\/identities$/);
    expect(authorization()).toBe(`Bearer ${accessToken}`);
  });

  it('gain one through a sign-in at the provider, confirmed with the password', async () => {
    respond({ available: true, authorization_url: 'https://idp.example.edu/authorize?state=l' });

    await expect(
      linkSsoIdentity({
        email: 'alice@example.edu',
        returnTo: '/dashboard/settings',
        password: 'Secret-1',
      }),
    ).resolves.toBe('https://idp.example.edu/authorize?state=l');

    expect(sent().url).toMatch(/\/auth\/sso\/identities\/link$/);
    expect(sent().body).toEqual({
      email: 'alice@example.edu',
      return_to: '/dashboard/settings',
      password: 'Secret-1',
    });
    expect(authorization()).toBe(`Bearer ${accessToken}`);
  });

  it('send no password after a confirmation at the provider', async () => {
    respond({ available: false, authorization_url: null });

    await expect(
      linkSsoIdentity({ email: 'alice@nowhere.test', returnTo: '/dashboard/settings' }),
    ).resolves.toBeNull();

    expect(sent().body).toEqual({ email: 'alice@nowhere.test', return_to: '/dashboard/settings' });
  });

  it('lose one, by its id', async () => {
    respond({ message: 'Single sign-on disconnected.' });

    await unlinkSsoIdentity('idn/1', { password: 'Secret-1' });

    expect(sent().url).toMatch(/\/auth\/sso\/identities\/idn%2F1\/unlink$/);
    expect(sent().options.method).toBe('POST');
    expect(sent().body).toEqual({ password: 'Secret-1' });
    expect(authorization()).toBe(`Bearer ${accessToken}`);
  });

  it('send an empty body after a confirmation at the provider', async () => {
    respond({ message: 'Single sign-on disconnected.' });

    await unlinkSsoIdentity('idn_1');

    expect(sent().body).toEqual({});
  });

  it('keep the gateway’s reason when the identity is the only way to sign in', async () => {
    refuse(409, 'This is the only way to sign in to this account. Set a password first.');

    const refusal = await unlinkSsoIdentity('idn_1').catch((error: unknown) => error);

    expect(ssoRefusalMessage(refusal)).toBe(
      'This is the only way to sign in to this account. Set a password first.',
    );
  });
});

describe('what a round trip came back with', () => {
  it('reads an error code, and takes an unknown one for a plain failure', () => {
    expect(ssoErrorCode(null)).toBeNull();
    expect(ssoErrorCode('')).toBeNull();
    expect(ssoErrorCode('cancelled')).toBe('cancelled');
    expect(ssoErrorCode('browser_full')).toBe('browser_full');
    expect(ssoErrorCode('from_a_later_gateway')).toBe('failed');
  });

  it('reads a confirmed purpose, and nothing else', () => {
    expect(ssoReauthPurpose('unlink_sso')).toBe('unlink_sso');
    expect(ssoReauthPurpose('set_password')).toBe('set_password');
    expect(ssoReauthPurpose('transfer_funds')).toBeNull();
    expect(ssoReauthPurpose(null)).toBeNull();
  });

  it('prefers the gateway’s sentence only for a refusal', () => {
    expect(ssoRefusalMessage(new APIError('UNKNOWN_ERROR', 'Wrong password.', 400))).toBe(
      'Wrong password.',
    );
    expect(ssoRefusalMessage(new APIError('SERVER_ERROR', 'HTTP 500', 500))).toBeNull();
    expect(ssoRefusalMessage(new APIError('NETWORK_ERROR', 'Failed to fetch'))).toBeNull();
    expect(ssoRefusalMessage(new Error('boom'))).toBeNull();
  });
});
