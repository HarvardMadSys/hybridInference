import { NextRequest } from 'next/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GET, HEAD, PATCH, POST } from './route';

const WEB = 'http://agent-web:3000';
const API = 'http://agent-control-plane:8000';

type Upstream = (url: string, init: RequestInit) => Response | Promise<Response>;

function stubFetch(handler: Upstream = () => new Response('upstream')) {
  const upstream = vi.fn(handler);
  vi.stubGlobal('fetch', upstream);
  return upstream;
}

function request(
  path = '/agents',
  init: { method?: string; body?: string; headers?: Record<string, string> } = {},
) {
  return new NextRequest(`https://freeinference.org${path}`, {
    method: init.method || 'GET',
    headers: { host: 'freeinference.org', ...init.headers },
    ...(init.body === undefined ? {} : { body: init.body }),
  });
}

describe('/agents runtime proxy', () => {
  beforeEach(() => {
    vi.stubEnv('AGENT_WEB_INTERNAL_URL', WEB);
    vi.stubEnv('AGENT_CONTROL_PLANE_INTERNAL_URL', API);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it('keeps the /agents prefix for the web app and preserves the query', async () => {
    const upstream = stubFetch();

    await GET(request('/agents/jobs/job-1?tab=output'));

    expect(String(upstream.mock.calls[0][0])).toBe(
      'http://agent-web:3000/agents/jobs/job-1?tab=output',
    );
  });

  it('strips /agents/api before forwarding to the control plane', async () => {
    const upstream = stubFetch();

    await GET(request('/agents/api/v1/jobs/job-1/events?after=3'));

    expect(String(upstream.mock.calls[0][0])).toBe(
      'http://agent-control-plane:8000/v1/jobs/job-1/events?after=3',
    );
  });

  it('maps the bare API prefix to the control-plane root', async () => {
    const upstream = stubFetch();

    await GET(request('/agents/api'));

    expect(String(upstream.mock.calls[0][0])).toBe('http://agent-control-plane:8000/');
  });

  it.each([
    ['AGENT_WEB_INTERNAL_URL', 'AGENT_CONTROL_PLANE_INTERNAL_URL'],
    ['AGENT_CONTROL_PLANE_INTERNAL_URL', 'AGENT_WEB_INTERNAL_URL'],
  ])('answers 404 when %s is the only configured target', async (present, absent) => {
    vi.stubEnv(present, present === 'AGENT_WEB_INTERNAL_URL' ? WEB : API);
    vi.stubEnv(absent, '');
    const upstream = stubFetch();

    const response = await GET(request('/agents'));

    expect(response.status).toBe(404);
    await expect(response.text()).resolves.toBe('Not found.');
    expect(upstream).not.toHaveBeenCalled();
  });

  it('answers 404 when neither target is configured', async () => {
    vi.stubEnv('AGENT_WEB_INTERNAL_URL', '');
    vi.stubEnv('AGENT_CONTROL_PLANE_INTERNAL_URL', '');
    const upstream = stubFetch();

    const response = await GET(request('/agents'));

    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it('answers 502 without exposing a malformed internal target', async () => {
    vi.stubEnv('AGENT_WEB_INTERNAL_URL', 'not a URL with an internal password');
    const upstream = stubFetch();

    const response = await GET(request('/agents'));

    expect(response.status).toBe(502);
    expect(await response.text()).toBe('Agent service is not reachable.');
    expect(upstream).not.toHaveBeenCalled();
  });

  it('answers 502 without exposing the target when the service cannot be reached', async () => {
    stubFetch(() => {
      throw new Error(`connect ECONNREFUSED ${WEB}`);
    });

    const response = await GET(request('/agents'));

    expect(response.status).toBe(502);
    const body = await response.text();
    expect(body).toBe('Agent service is not reachable.');
    expect(body).not.toContain(WEB);
  });

  it('streams the request body and preserves non-GET methods', async () => {
    const upstream = stubFetch();

    await POST(request('/agents/api/v1/jobs', { method: 'POST', body: '{"model":"x"}' }));

    const init = upstream.mock.calls[0][1];
    expect(init.method).toBe('POST');
    expect((init as RequestInit & { duplex?: string }).duplex).toBe('half');
    await expect(new Response(init.body).text()).resolves.toBe('{"model":"x"}');
  });

  it('supports another body method and HEAD without inventing a body', async () => {
    const upstream = stubFetch();

    await PATCH(request('/agents/api/v1/jobs/job-1', { method: 'PATCH', body: 'patch' }));
    await HEAD(request('/agents/health', { method: 'HEAD' }));

    expect(upstream.mock.calls[0][1].method).toBe('PATCH');
    expect(upstream.mock.calls[1][1]).toMatchObject({ method: 'HEAD', body: undefined });
  });

  it('sets public forwarding headers and drops connection-scoped request headers', async () => {
    const upstream = stubFetch();

    await GET(
      request('/agents', {
        headers: {
          connection: 'keep-alive, x-private-hop',
          forwarded: 'host=attacker.invalid;proto=http',
          'x-forwarded-host': 'attacker.invalid',
          'x-forwarded-port': '1234',
          'x-forwarded-proto': 'http',
          'x-private-hop': 'remove-me',
          'x-request-id': 'keep-me',
        },
      }),
    );

    const headers = new Headers(upstream.mock.calls[0][1].headers);
    expect(headers.get('host')).toBeNull();
    expect(headers.get('connection')).toBeNull();
    expect(headers.get('x-private-hop')).toBeNull();
    expect(headers.get('forwarded')).toBeNull();
    expect(headers.get('x-request-id')).toBe('keep-me');
    expect(headers.get('x-forwarded-host')).toBe('freeinference.org');
    expect(headers.get('x-forwarded-port')).toBe('443');
    expect(headers.get('x-forwarded-proto')).toBe('https');
  });

  it('preserves separate Set-Cookie fields and drops encoded and hop-by-hop response headers', async () => {
    const headers = new Headers({
      connection: 'x-private-hop',
      'content-encoding': 'gzip',
      'content-length': '999',
      'content-type': 'application/json',
      'x-private-hop': 'remove-me',
    });
    headers.append('set-cookie', 'agent_session=one; Path=/agents; HttpOnly');
    headers.append('set-cookie', 'agent_csrf=two; Path=/agents');
    stubFetch(() => new Response('{}', { headers }));

    const response = await GET(request('/agents/api/v1/session'));

    expect(response.headers.getSetCookie()).toEqual([
      'agent_session=one; Path=/agents; HttpOnly',
      'agent_csrf=two; Path=/agents',
    ]);
    expect(response.headers.get('content-encoding')).toBeNull();
    expect(response.headers.get('content-length')).toBeNull();
    expect(response.headers.get('connection')).toBeNull();
    expect(response.headers.get('x-private-hop')).toBeNull();
    expect(response.headers.get('content-type')).toBe('application/json');
  });

  it('folds internal redirects back through the public API prefix', async () => {
    stubFetch(
      () =>
        new Response(null, {
          status: 307,
          headers: { location: 'http://agent-control-plane:9000/v1/jobs/job-2?view=1' },
        }),
    );

    const response = await GET(request('/agents/api/v1/jobs/job-1'));

    expect(response.headers.get('location')).toBe('/agents/api/v1/jobs/job-2?view=1');
    expect(response.headers.get('location')).not.toContain('agent-control-plane');
  });

  it('folds a control-plane redirect to the configured web service', async () => {
    stubFetch(
      () =>
        new Response(null, {
          status: 303,
          headers: { location: 'http://agent-web:3000/agents/jobs/job-2' },
        }),
    );

    const response = await GET(request('/agents/api/v1/jobs/job-1'));

    expect(response.headers.get('location')).toBe('/agents/jobs/job-2');
    expect(response.headers.get('location')).not.toContain('agent-web');
  });

  it('passes the first SSE chunk through before the delayed second chunk exists', async () => {
    vi.useFakeTimers();
    const encoder = new TextEncoder();
    stubFetch(
      () =>
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(encoder.encode('event: output\ndata: first\n\n'));
              setTimeout(() => {
                controller.enqueue(encoder.encode('event: output\ndata: second\n\n'));
                controller.close();
              }, 100);
            },
          }),
          { headers: { 'content-type': 'text/event-stream' } },
        ),
    );

    const response = await GET(request('/agents/api/v1/jobs/job-1/events'));
    const reader = response.body!.getReader();
    const first = await reader.read();

    expect(new TextDecoder().decode(first.value)).toBe('event: output\ndata: first\n\n');

    let secondArrived = false;
    const secondRead = reader.read().then((value) => {
      secondArrived = true;
      return value;
    });
    await vi.advanceTimersByTimeAsync(99);
    expect(secondArrived).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const second = await secondRead;
    expect(new TextDecoder().decode(second.value)).toBe('event: output\ndata: second\n\n');
  });
});
