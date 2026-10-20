// @vitest-environment node
import http from 'node:http';
import type { AddressInfo } from 'node:net';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Handler = (req: http.IncomingMessage, res: http.ServerResponse) => void;

let server: http.Server;
let handler: Handler;
let proxyToGateway: (request: Request) => Promise<Response>;

beforeEach(async () => {
  server = http.createServer((req, res) => handler(req, res));
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  // The target is read once, at import, as it is from the build in production.
  vi.stubEnv('BUILT_BACKEND_INTERNAL_URL', `http://127.0.0.1:${port}/`);
  vi.resetModules();
  ({ proxyToGateway } = await import('./gatewayProxy'));
});

afterEach(async () => {
  vi.unstubAllEnvs();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
});

function callerRequest(path: string, init: RequestInit = {}): Request {
  return new Request(`https://console.example.org${path}`, init);
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => resolve(body));
  });
}

describe('proxyToGateway', () => {
  it('relays the request and the response as they are', async () => {
    let seen: { method?: string; url?: string; headers: http.IncomingHttpHeaders; body: string } = {
      headers: {},
      body: '',
    };
    handler = async (req, res) => {
      seen = { method: req.method, url: req.url, headers: req.headers, body: await readBody(req) };
      res.writeHead(201, 'Made', [
        'content-type',
        'application/json',
        'set-cookie',
        'a=1',
        'set-cookie',
        'b=2',
        'content-encoding',
        'identity',
        'x-request-id',
        'r1',
      ]);
      res.end('{"ok":true}');
    };

    const response = await proxyToGateway(
      callerRequest('/v1/chat/completions?x=1&y=2', {
        method: 'POST',
        headers: {
          authorization: 'Bearer key',
          'content-type': 'application/json',
          'cf-connecting-ip': '203.0.113.7',
          'x-forwarded-for': '203.0.113.7',
          connection: 'keep-alive, x-hop',
          'x-hop': 'connection-scoped',
          te: 'trailers',
        },
        body: '{"model":"m"}',
      }),
    );

    expect(seen.method).toBe('POST');
    expect(seen.url).toBe('/v1/chat/completions?x=1&y=2');
    expect(seen.body).toBe('{"model":"m"}');
    expect(seen.headers.authorization).toBe('Bearer key');
    expect(seen.headers['cf-connecting-ip']).toBe('203.0.113.7');
    expect(seen.headers['x-forwarded-for']).toBe('203.0.113.7');
    expect(seen.headers['x-forwarded-host']).toBe('console.example.org');
    expect(seen.headers.host).toMatch(/^127\.0\.0\.1:\d+$/);
    // Connection-scoped: named by Connection, or hop-by-hop by definition.
    expect(seen.headers['x-hop']).toBeUndefined();
    expect(seen.headers.te).toBeUndefined();

    expect(response.status).toBe(201);
    expect(response.statusText).toBe('Made');
    expect(response.headers.get('x-request-id')).toBe('r1');
    expect(response.headers.get('content-encoding')).toBe('identity');
    expect(response.headers.getSetCookie()).toEqual(['a=1', 'b=2']);
    expect(await response.text()).toBe('{"ok":true}');
  });

  it('streams each chunk as the gateway sends it', async () => {
    let finish: () => void = () => {};
    handler = (_req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: first\n\n');
      finish = () => res.end('data: [DONE]\n\n');
    };

    const response = await proxyToGateway(callerRequest('/v1/chat/completions'));
    const reader = response.body!.getReader();
    const first = await reader.read();

    expect(new TextDecoder().decode(first.value)).toBe('data: first\n\n');
    finish();
    const rest = await reader.read();
    expect(new TextDecoder().decode(rest.value)).toBe('data: [DONE]\n\n');
    expect((await reader.read()).done).toBe(true);
  });

  it('breaks the response when the gateway drops mid-stream, never ends it cleanly', async () => {
    handler = (_req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: partial\n\n', () => setTimeout(() => res.socket?.destroy(), 20));
    };

    const response = await proxyToGateway(callerRequest('/internal/playground/chat'));
    const reader = response.body!.getReader();
    expect(new TextDecoder().decode((await reader.read()).value)).toBe('data: partial\n\n');

    await expect(reader.read()).rejects.toThrow();
  });

  it('stops the gateway’s work when the caller hangs up', async () => {
    const gatewaySawClose = new Promise<void>((resolve) => {
      handler = (req, res) => {
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.write('data: first\n\n');
        res.on('close', () => resolve());
        void req;
      };
    });
    const caller = new AbortController();

    const response = await proxyToGateway(
      callerRequest('/anthropic/v1/messages', { signal: caller.signal }),
    );
    await response.body!.getReader().read();
    caller.abort();

    await expect(gatewaySawClose).resolves.toBeUndefined();
  });

  it('answers a JSON 502 when the gateway cannot be reached', async () => {
    await new Promise((resolve) => server.close(resolve));
    const response = await proxyToGateway(callerRequest('/v1/models'));

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: { message: 'The gateway is not reachable.', type: 'server_error', code: 502 },
    });
    server = http.createServer();
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  });

  it.each([
    ['HEAD', 200],
    ['GET', 204],
    ['GET', 304],
  ])('passes a %s %i on without a body', async (method, status) => {
    handler = (_req, res) => {
      res.writeHead(status, { 'x-kept': 'yes' });
      res.end();
    };

    const response = await proxyToGateway(callerRequest('/v1/models', { method }));

    expect(response.status).toBe(status);
    expect(response.headers.get('x-kept')).toBe('yes');
    expect(response.body).toBeNull();
  });
});
