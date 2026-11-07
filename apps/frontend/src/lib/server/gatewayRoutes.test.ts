// @vitest-environment node
/**
 * The route files that relay to the gateway: each answers every method with one
 * relay, mounted at its own URL prefix, and relays nothing outside it.
 *
 * `/user/batch-files` has a handler of its own because an upload is larger than
 * a rewrite forwards: Next copies every body it forwards through a rewrite,
 * holds the copy in memory, and cuts it off at `middlewareClientMaxBodySize`.
 */
import http from 'node:http';
import type { AddressInfo } from 'node:net';

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'] as const;

type RouteModule = Record<(typeof METHODS)[number], (request: Request) => Promise<Response>> & {
  dynamic: string;
  runtime: string;
};

const ROUTES: Array<[mount: string, load: () => Promise<RouteModule>]> = [
  ['/v1', () => import('@/app/v1/[[...path]]/route') as Promise<RouteModule>],
  ['/anthropic', () => import('@/app/anthropic/[[...path]]/route') as Promise<RouteModule>],
  [
    '/internal/playground',
    () => import('@/app/internal/playground/[[...path]]/route') as Promise<RouteModule>,
  ],
  [
    '/user/batch-files',
    () => import('@/app/user/batch-files/[[...path]]/route') as Promise<RouteModule>,
  ],
];

let server: http.Server;
const seen: string[] = [];

beforeAll(async () => {
  server = http.createServer((req, res) => {
    seen.push(req.url ?? '');
    res.end('ok');
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  vi.stubEnv('BUILT_BACKEND_INTERNAL_URL', `http://127.0.0.1:${port}`);
  vi.resetModules();
});

afterAll(async () => {
  vi.unstubAllEnvs();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
});

describe.each(ROUTES)('the %s route file', (mount, load) => {
  it('answers every method with one relay, on the Node runtime', async () => {
    const route = await load();
    for (const method of METHODS) expect(route[method], method).toBe(route.GET);
    expect(route.runtime).toBe('nodejs');
    expect(route.dynamic).toBe('force-dynamic');
  });

  it('relays a path under its prefix', async () => {
    const route = await load();
    seen.length = 0;

    const response = await route.GET(new Request(`https://console.example.org${mount}/x?y=1`));

    expect(response.status).toBe(200);
    expect(seen).toEqual([`${mount}/x?y=1`]);
  });

  it('refuses a path whose dot segments lead outside its prefix', async () => {
    const route = await load();
    seen.length = 0;

    for (const path of [
      `${mount}/%2e%2e/openapi.json`,
      `${mount}/../health/deep`,
      `${mount}/.%2E/%2e%2e/routing`,
      `${mount}/x/%2e%2e/%2e%2e/docs`,
    ]) {
      const response = await route.POST(
        new Request(`https://console.example.org${path}`, { method: 'POST', body: '{}' }),
      );
      expect(response.status, path).toBe(404);
      expect(await response.json(), path).toEqual({
        error: { message: 'Not found.', type: 'invalid_request_error', code: 404 },
      });
    }
    expect(seen).toEqual([]);
  });
});
