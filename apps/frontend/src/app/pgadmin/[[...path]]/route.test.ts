/**
 * Whether pgAdmin asks for a login of its own is a per-host setting, so this
 * route has to hold as if it were the only thing between the public internet
 * and a database console. These tests pin the deny paths first, and every one
 * of them also proves the proxy was never called: a gate that answers 403
 * while still forwarding the request is no gate at all.
 */
import { NextRequest } from 'next/server';
import { describe, expect, it, vi } from 'vitest';

import { GET, POST } from './route';

const VERIFY_URL = '/internal/verify-admin';

type Upstream = (url: string, init: RequestInit) => Response | Promise<Response>;

/**
 * Route both outbound calls the handler can make. `verify` stands in for
 * FastAPI, `upstream` for pgAdmin; either may be omitted by a test that
 * expects it never to be reached.
 */
function stubFetch(handlers: { verify?: () => Response | Promise<Response>; upstream?: Upstream }) {
  const verify = vi.fn(handlers.verify ?? (() => new Response(null, { status: 500 })));
  const upstream = vi.fn(handlers.upstream ?? (() => new Response('pgadmin', { status: 200 })));

  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      return url.includes(VERIFY_URL) ? verify() : upstream(url, init);
    }),
  );

  return { verify, upstream };
}

function request(
  path = '/pgadmin/browser/',
  init: { method?: string; cookie?: string; headers?: Record<string, string> } = {},
) {
  const headers = new Headers({ host: 'freeinference.org', ...init.headers });
  if (init.cookie) headers.set('cookie', init.cookie);
  return new NextRequest(`https://freeinference.org${path}`, {
    method: init.method ?? 'GET',
    headers,
    ...(init.method === 'POST' ? { body: 'payload' } : {}),
  });
}

describe('pgAdmin proxy — denial', () => {
  it('sends a request with no session to the login page, without asking the backend', async () => {
    const { verify, upstream } = stubFetch({});

    const response = await GET(request());

    expect(response.status).toBe(302);
    // Relative on purpose: an absolute URL is built from the Host this app
    // sees, which behind the tunnel is its own bind address.
    expect(response.headers.get('location')).toBe('/login');
    expect(verify).not.toHaveBeenCalled();
    expect(upstream).not.toHaveBeenCalled();
  });

  it('sends an expired session to the login page', async () => {
    const { upstream } = stubFetch({ verify: () => new Response(null, { status: 401 }) });

    const response = await GET(request('/pgadmin/', { cookie: 'refresh_token=stale' }));

    expect(response.status).toBe(302);
    expect(upstream).not.toHaveBeenCalled();
  });

  it('refuses a signed-in non-admin', async () => {
    const { upstream } = stubFetch({ verify: () => new Response(null, { status: 403 }) });

    const response = await GET(request('/pgadmin/', { cookie: 'refresh_token=user' }));

    expect(response.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });

  it('refuses when the backend cannot be reached, rather than falling through', async () => {
    const { upstream } = stubFetch({
      verify: () => {
        throw new Error('ECONNREFUSED');
      },
    });

    const response = await GET(request('/pgadmin/', { cookie: 'refresh_token=admin' }));

    expect(response.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });

  it('refuses on any unexpected verdict from the backend', async () => {
    const { upstream } = stubFetch({ verify: () => new Response(null, { status: 500 }) });

    const response = await GET(request('/pgadmin/', { cookie: 'refresh_token=admin' }));

    expect(response.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });
});

describe('pgAdmin proxy — forwarding', () => {
  const admin = { cookie: 'refresh_token=admin; pga4_session=abc' };

  it('passes an admin through, preserving path and query', async () => {
    const { upstream } = stubFetch({
      verify: () => new Response(null, { status: 200 }),
      upstream: () => new Response('<html>pgadmin</html>', { status: 200 }),
    });

    const response = await GET(request('/pgadmin/browser/js/utils.js?ver=8', admin));

    expect(response.status).toBe(200);
    await expect(response.text()).resolves.toBe('<html>pgadmin</html>');
    expect(upstream).toHaveBeenCalledOnce();
    // URL drops the default port, so the :80 in the env default disappears here.
    expect(upstream.mock.calls[0][0]).toBe('http://pgadmin/pgadmin/browser/js/utils.js?ver=8');
  });

  it('keeps pgAdmin’s own cookie but not the console session', async () => {
    const { upstream } = stubFetch({ verify: () => new Response(null, { status: 200 }) });

    await GET(request('/pgadmin/', admin));

    const sent = new Headers(upstream.mock.calls[0][1].headers as HeadersInit).get('cookie');
    expect(sent).toBe('pga4_session=abc');
  });

  it('strips a deployment-scoped console session cookie', async () => {
    vi.stubEnv('REFRESH_TOKEN_COOKIE_NAME', 'hybridinference_example_refresh');
    const { upstream } = stubFetch({ verify: () => new Response(null, { status: 200 }) });

    await GET(
      request('/pgadmin/', {
        cookie: 'hybridinference_example_refresh=admin; pga4_session=abc',
      }),
    );

    const sent = new Headers(upstream.mock.calls[0][1].headers as HeadersInit).get('cookie');
    expect(sent).toBe('pga4_session=abc');
    vi.unstubAllEnvs();
  });

  it('strips both default and deployment-scoped console sessions', async () => {
    vi.stubEnv('REFRESH_TOKEN_COOKIE_NAME', 'hybridinference_example_refresh');
    const { upstream } = stubFetch({ verify: () => new Response(null, { status: 200 }) });

    await GET(
      request('/pgadmin/', {
        cookie:
          'refresh_token=another-stack; hybridinference_example_refresh=admin; pga4_session=abc',
      }),
    );

    const sent = new Headers(upstream.mock.calls[0][1].headers as HeadersInit).get('cookie');
    expect(sent).toBe('pga4_session=abc');
    vi.unstubAllEnvs();
  });

  it('forwards the method for a non-GET request', async () => {
    const { upstream } = stubFetch({ verify: () => new Response(null, { status: 200 }) });

    await POST(request('/pgadmin/login', { ...admin, method: 'POST' }));

    expect(upstream.mock.calls[0][1].method).toBe('POST');
  });

  it('drops content-encoding, which fetch has already applied', async () => {
    stubFetch({
      verify: () => new Response(null, { status: 200 }),
      upstream: () =>
        new Response('plain bytes', {
          status: 200,
          headers: { 'content-encoding': 'gzip', 'content-type': 'text/html' },
        }),
    });

    const response = await GET(request('/pgadmin/', admin));

    expect(response.headers.get('content-encoding')).toBeNull();
    expect(response.headers.get('content-type')).toBe('text/html');
  });

  it('folds a redirect aimed at the container back to a path', async () => {
    // The container name resolves nowhere in a browser, so an absolute
    // redirect built from the Host pgAdmin saw would strand the client.
    stubFetch({
      verify: () => new Response(null, { status: 200 }),
      upstream: () =>
        new Response(null, {
          status: 302,
          headers: { location: 'http://pgadmin/pgadmin/browser/?x=1' },
        }),
    });

    const response = await GET(request('/pgadmin/', admin));

    expect(response.headers.get('location')).toBe('/pgadmin/browser/?x=1');
  });

  it('folds a redirect that names the container on some other port', async () => {
    // What production actually served: pgAdmin's ProxyFix had taken the port
    // out of x-forwarded-port, so the container it named was `pgadmin:3001`,
    // and matching the upstream by origin let it straight through.
    stubFetch({
      verify: () => new Response(null, { status: 200 }),
      upstream: () =>
        new Response(null, {
          status: 308,
          headers: { location: 'http://pgadmin:3001/pgadmin/' },
        }),
    });

    const response = await GET(request('/pgadmin', admin));

    expect(response.headers.get('location')).toBe('/pgadmin/');
  });

  it('withholds the port Next fills in for itself, which pgAdmin would trust', async () => {
    // Left in place, pgAdmin answers a slash-less path with a 308 to
    // `http://pgadmin:3001/…` — the console's own port on a name only Docker
    // can resolve.
    const { upstream } = stubFetch({ verify: () => new Response(null, { status: 200 }) });

    await GET(request('/pgadmin', { ...admin, headers: { 'x-forwarded-port': '3001' } }));

    const sent = new Headers(upstream.mock.calls[0][1].headers as HeadersInit);
    expect(sent.get('x-forwarded-port')).toBeNull();
    expect(sent.get('x-forwarded-host')).toBe('freeinference.org');
  });

  it('leaves a redirect that already is a path alone', async () => {
    stubFetch({
      verify: () => new Response(null, { status: 200 }),
      upstream: () => new Response(null, { status: 302, headers: { location: '/pgadmin/login' } }),
    });

    const response = await GET(request('/pgadmin/', admin));

    expect(response.headers.get('location')).toBe('/pgadmin/login');
  });

  it('reports a stopped pgAdmin as a bad gateway', async () => {
    stubFetch({
      verify: () => new Response(null, { status: 200 }),
      upstream: () => {
        throw new Error('ECONNREFUSED');
      },
    });

    const response = await GET(request('/pgadmin/', admin));

    expect(response.status).toBe(502);
  });
});
