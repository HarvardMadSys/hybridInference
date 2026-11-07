// @vitest-environment node
/**
 * The route files that relay to the gateway: each answers every method with one
 * relay, mounted at its own URL prefix, and relays nothing outside it.
 *
 * Every gateway path that takes a request body is one of these rather than a
 * rewrite: Next copies every body it forwards through a rewrite into memory and
 * cuts it off at `middlewareClientMaxBodySize`, while these stream it.
 */
import { readdirSync } from 'node:fs';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { join, sep } from 'node:path';

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'] as const;

type RouteModule = Record<(typeof METHODS)[number], (request: Request) => Promise<Response>> & {
  dynamic: string;
  runtime: string;
};

/** Each gateway route file under src/app, the prefix it relays, and its module. */
const ROUTES: Array<[file: string, mount: string, load: () => Promise<unknown>]> = [
  ['v1/[[...path]]', '/v1', () => import('@/app/v1/[[...path]]/route')],
  ['anthropic/[[...path]]', '/anthropic', () => import('@/app/anthropic/[[...path]]/route')],
  ['auth/[[...path]]', '/auth', () => import('@/app/auth/[[...path]]/route')],
  ['user/[[...path]]', '/user', () => import('@/app/user/[[...path]]/route')],
  ['admin/[[...path]]', '/admin', () => import('@/app/admin/[[...path]]/route')],
  [
    'internal/playground/[[...path]]',
    '/internal/playground',
    () => import('@/app/internal/playground/[[...path]]/route'),
  ],
  [
    'internal/verify-admin',
    '/internal/verify-admin',
    () => import('@/app/internal/verify-admin/route'),
  ],
  [
    'internal/model-catalog',
    '/internal/model-catalog',
    () => import('@/app/internal/model-catalog/route'),
  ],
  [
    'internal/users/[userId]/status',
    '/internal/users',
    () => import('@/app/internal/users/[userId]/status/route'),
  ],
  [
    'internal/users/[userId]/agent-access',
    '/internal/users',
    () => import('@/app/internal/users/[userId]/agent-access/route'),
  ],
  [
    'internal/agent-grants/[[...path]]',
    '/internal/agent-grants',
    () => import('@/app/internal/agent-grants/[[...path]]/route'),
  ],
  [
    'internal/alert-history/events',
    '/internal/alert-history/events',
    () => import('@/app/internal/alert-history/events/route'),
  ],
];

/** The gateway's prefixes, under which every route file must be listed above. */
const GATEWAY_PREFIXES = ['v1', 'anthropic', 'auth', 'user', 'admin', 'internal'];

function routeFilesUnder(dir: string): string[] {
  const app = join(__dirname, '../../app');
  return readdirSync(join(app, dir), { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return routeFilesUnder(path);
    return entry.name === 'route.ts' ? [dir.split(sep).join('/')] : [];
  });
}

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

it('lists every route file under the gateway’s prefixes', () => {
  expect(GATEWAY_PREFIXES.flatMap(routeFilesUnder).sort()).toEqual(
    ROUTES.map(([file]) => file).sort(),
  );
});

describe.each(ROUTES)('src/app/%s', (_file, mount, loadModule) => {
  const load = () => loadModule() as Promise<RouteModule>;

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
