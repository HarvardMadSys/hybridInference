// @vitest-environment node
import http from 'node:http';
import type { AddressInfo } from 'node:net';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Handler = (req: http.IncomingMessage, res: http.ServerResponse) => void;

let server: http.Server;
let handler: Handler;
let proxyToGateway: (request: Request, mount: string, idleTimeoutMs?: number) => Promise<Response>;
/** Which of the server's connections a request arrived on, numbered from 0. */
let connectionOf: (req: http.IncomingMessage) => number;

beforeEach(async () => {
  server = http.createServer((req, res) => handler(req, res));
  const connections = new WeakMap<object, number>();
  let accepted = 0;
  server.on('connection', (socket) => connections.set(socket, accepted++));
  connectionOf = (req) => connections.get(req.socket) ?? -1;
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
      '/v1',
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

    const response = await proxyToGateway(callerRequest('/v1/chat/completions'), '/v1');
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

    const response = await proxyToGateway(
      callerRequest('/internal/playground/chat'),
      '/internal/playground',
    );
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
      '/anthropic',
    );
    await response.body!.getReader().read();
    caller.abort();

    await expect(gatewaySawClose).resolves.toBeUndefined();
  });

  it('answers a JSON 502 when the gateway cannot be reached', async () => {
    await new Promise((resolve) => server.close(resolve));
    const response = await proxyToGateway(callerRequest('/v1/models'), '/v1');

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

    const response = await proxyToGateway(callerRequest('/v1/models', { method }), '/v1');

    expect(response.status).toBe(status);
    expect(response.headers.get('x-kept')).toBe('yes');
    expect(response.body).toBeNull();
  });
});

// A handler's `request.url` arrives with its dot segments resolved, so the path
// relayed is the one that URL names, and it has to stay under the handler's own
// prefix.
describe('the prefix a handler relays', () => {
  it('takes the prefix itself and any path under it, as the URL resolves it', async () => {
    const seen: string[] = [];
    handler = (req, res) => {
      seen.push(req.url ?? '');
      res.end();
    };

    for (const path of ['/v1', '/v1/models', '/v1/x/%2e%2e/models', '/v1/./models?a=1']) {
      expect((await proxyToGateway(callerRequest(path), '/v1')).status, path).toBe(200);
    }
    expect(seen).toEqual(['/v1', '/v1/models', '/v1/models', '/v1/models?a=1']);
  });

  it('refuses a path outside it, a neighbour sharing its first letters included', async () => {
    handler = () => {
      throw new Error('nothing outside the prefix may reach the gateway');
    };

    for (const path of ['/v1/%2e%2e/openapi.json', '/v1/%2e%2e/v1beta/models', '/v1/%2e%2e']) {
      const response = await proxyToGateway(callerRequest(path), '/v1');
      expect(response.status, path).toBe(404);
      expect(response.headers.get('cache-control')).toBe('no-store');
    }
  });
});

// The connection to the gateway is pooled. The gateway reads a request's body up
// to where that request's framing says it ends, and whatever follows on the
// connection as the next request -- whoever's it is. So the framing has to
// describe exactly the bytes this proxy writes.
describe('the pooled connection to the gateway', () => {
  type Seen = { method?: string; url?: string; headers: http.IncomingHttpHeaders; body: string };

  /** A gateway that, like the real one on `GET /v1/models`, never reads a GET's body. */
  function recordingGateway(): Seen[] {
    const seen: Seen[] = [];
    handler = async (req, res) => {
      const entry: Seen = { method: req.method, url: req.url, headers: req.headers, body: '' };
      seen.push(entry);
      if (req.method !== 'GET') entry.body = await readBody(req);
      res.writeHead(200, {
        'content-type': 'text/plain',
        'x-connection': String(connectionOf(req)),
      });
      res.end(`${req.method} ${req.url} ${entry.body}`);
    };
    return seen;
  }

  async function answer(response: Response): Promise<string> {
    const text = `${response.status} ${await response.text()}`;
    // Let the connection go back to the pool before the next request asks for one.
    await new Promise((resolve) => setImmediate(resolve));
    return text;
  }

  it('sends a GET that declares a length without it, so the next request is read as itself', async () => {
    const seen = recordingGateway();

    // Next hands a GET handler no body, but keeps the caller's headers.
    const attacker = await proxyToGateway(
      callerRequest('/v1/models', { method: 'GET', headers: { 'content-length': '60' } }),
      '/v1',
    );
    expect(await answer(attacker)).toBe('200 GET /v1/models ');
    const victim = await proxyToGateway(
      callerRequest('/v1/chat/completions', {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'content-length': '13' },
        body: '{"model":"m"}',
      }),
      '/v1',
    );

    expect(await answer(victim)).toBe('200 POST /v1/chat/completions {"model":"m"}');
    expect(seen.map((s) => `${s.method} ${s.url}`)).toEqual([
      'GET /v1/models',
      'POST /v1/chat/completions',
    ]);
    expect(seen[0].headers['content-length']).toBeUndefined();
    expect(seen[0].headers['transfer-encoding']).toBeUndefined();
    expect(seen[1].headers['content-length']).toBe('13');
    // One connection, reused: the case the framing has to get right.
    expect(attacker.headers.get('x-connection')).toBe(victim.headers.get('x-connection'));
  });

  it('frames a body that has no declared length, whatever the method', async () => {
    const seen = recordingGateway();
    const smuggled = 'GET /admin/users HTTP/1.1\r\nHost: backend\r\n\r\n';

    // Node does not send a DELETE chunked by default: unframed, this body
    // would be read as the next request.
    const first = await proxyToGateway(
      callerRequest('/v1/files/f1', {
        method: 'DELETE',
        headers: { 'transfer-encoding': 'chunked' },
        body: smuggled,
      }),
      '/v1',
    );
    expect(await answer(first)).toBe(`200 DELETE /v1/files/f1 ${smuggled}`);
    const second = await proxyToGateway(callerRequest('/v1/models'), '/v1');

    expect(await answer(second)).toBe('200 GET /v1/models ');
    expect(seen.map((s) => `${s.method} ${s.url}`)).toEqual([
      'DELETE /v1/files/f1',
      'GET /v1/models',
    ]);
    expect(seen[0].headers['transfer-encoding']).toBe('chunked');
    expect(first.headers.get('x-connection')).toBe(second.headers.get('x-connection'));
  });

  it('closes the connection rather than send a body that does not match its length', async () => {
    const seen = recordingGateway();

    const short = await proxyToGateway(
      callerRequest('/v1/chat/completions', {
        method: 'POST',
        headers: { 'content-length': '10' },
        body: 'abc',
      }),
      '/v1',
    );
    expect(short.status).toBe(502);
    await new Promise((resolve) => setImmediate(resolve));

    // Reused, that connection would hand the next request's first bytes to
    // the short one as the rest of its body.
    const next = await proxyToGateway(callerRequest('/v1/models'), '/v1');
    expect(await answer(next)).toBe('200 GET /v1/models ');
    expect(seen.at(-1)?.url).toBe('/v1/models');
  });

  /** A gateway that, like the real one refusing a key, answers on the headers alone. */
  function refusingGateway(onConnection: (socket: http.IncomingMessage['socket']) => void) {
    handler = (req, res) => {
      onConnection(req.socket);
      res.writeHead(429, {
        'content-type': 'text/plain',
        'x-connection': String(connectionOf(req)),
      });
      res.end('blocked');
    };
  }

  it('closes the connection of a request answered before its body was sent', async () => {
    // The gateway keeps such a connection for as long as the proxy does; Node's
    // server, standing in for it here, would otherwise close it after 5s.
    server.keepAliveTimeout = 0;
    const closed = new Promise<string>((resolve) => {
      refusingGateway((socket) => socket.once('close', () => resolve('closed')));
    });
    // A caller that left on reading the answer: Next reports neither an end
    // nor an error on its body, and does not abort `request.signal`.
    const unfinished = new ReadableStream<Uint8Array>({
      start: (controller) => controller.enqueue(new Uint8Array(1024)),
    });

    const response = await proxyToGateway(
      callerRequest('/v1/chat/completions', {
        method: 'POST',
        headers: { 'content-length': String(1024 * 1024) },
        body: unfinished,
        // A stream body requires it; the DOM types do not have it yet.
        duplex: 'half',
      } as RequestInit),
      '/v1',
    );
    expect(await answer(response)).toBe('429 blocked');

    const stillOpen = new Promise<string>((resolve) => setTimeout(resolve, 1000, 'still open'));
    expect(await Promise.race([closed, stillOpen])).toBe('closed');
  });

  it('closes it too when the caller sends on, and reads no more of that body', async () => {
    server.keepAliveTimeout = 0;
    const closed = new Promise<string>((resolve) => {
      refusingGateway((socket) => socket.once('close', () => resolve('closed')));
    });
    // Once the response is complete Node no longer says when the connection
    // will take more, so a body that had to wait for the gateway would wait
    // for good.
    const cancel = vi.fn();
    const endless = new ReadableStream<Uint8Array>({
      pull: (controller) => controller.enqueue(new Uint8Array(64 * 1024)),
      cancel,
    });

    const response = await proxyToGateway(
      callerRequest('/v1/chat/completions', {
        method: 'POST',
        body: endless,
        duplex: 'half',
      } as RequestInit),
      '/v1',
    );
    expect(await answer(response)).toBe('429 blocked');

    const stillOpen = new Promise<string>((resolve) => setTimeout(resolve, 1000, 'still open'));
    expect(await Promise.race([closed, stillOpen])).toBe('closed');
    await vi.waitFor(() => expect(cancel).toHaveBeenCalled());
  });

  it('reuses the connection of a request answered early whose body was sent whole', async () => {
    refusingGateway(() => {});
    const refused = () =>
      proxyToGateway(
        callerRequest('/v1/chat/completions', {
          method: 'POST',
          headers: { 'content-type': 'application/json', 'content-length': '13' },
          body: '{"model":"m"}',
        }),
        '/v1',
      );

    const first = await refused();
    expect(await answer(first)).toBe('429 blocked');
    const second = await refused();
    expect(await answer(second)).toBe('429 blocked');

    expect(second.headers.get('x-connection')).toBe(first.headers.get('x-connection'));
  });

  it('sends a large body whole, at the pace the gateway reads it', async () => {
    const size = 8 * 1024 * 1024;
    handler = (req, res) => {
      let received = 0;
      // Read in bursts, so the proxy has to wait for the gateway to catch up.
      req.on('data', (chunk: Buffer) => {
        received += chunk.length;
        req.pause();
        setTimeout(() => req.resume(), 1);
      });
      req.on('end', () => res.end(`${req.headers['content-length']} ${received}`));
    };

    const response = await proxyToGateway(
      callerRequest('/user/batch-files', {
        method: 'POST',
        headers: { 'content-length': String(size) },
        body: new Uint8Array(size).fill(0x61),
      }),
      '/user/batch-files',
    );

    expect(await answer(response)).toBe(`200 ${size} ${size}`);
  });

  it('answers 504 when the gateway stays silent past the idle limit', async () => {
    handler = () => {};

    const response = await proxyToGateway(callerRequest('/v1/chat/completions'), '/v1', 50);

    expect(response.status).toBe(504);
    expect(await response.json()).toEqual({
      error: { message: 'The gateway stopped responding.', type: 'server_error', code: 504 },
    });
  });

  it('breaks a response that goes silent past the idle limit, and keeps a slow one', async () => {
    handler = (req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      if (req.url === '/v1/stalls') {
        res.write('data: first\n\n');
        return;
      }
      // Each write comes well inside the idle limit; the whole response does not.
      let sent = 0;
      const timer = setInterval(() => {
        res.write(': keepalive\n\n');
        if (++sent === 8) {
          clearInterval(timer);
          res.end('data: [DONE]\n\n');
        }
      }, 25);
    };

    const stalled = await proxyToGateway(callerRequest('/v1/stalls'), '/v1', 100);
    const reader = stalled.body!.getReader();
    expect(new TextDecoder().decode((await reader.read()).value)).toBe('data: first\n\n');
    await expect(reader.read()).rejects.toThrow();

    const kept = await proxyToGateway(callerRequest('/v1/keeps'), '/v1', 100);
    expect(await kept.text()).toBe(`${': keepalive\n\n'.repeat(8)}data: [DONE]\n\n`);
  });
});
