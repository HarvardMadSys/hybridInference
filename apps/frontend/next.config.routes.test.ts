// A rewrite must never take a request that one of the app's route handlers is
// there to serve.
//
// Next checks `afterFiles` rewrites before *dynamic* routes: a handler under a
// `[param]` or `[[...path]]` folder runs only when no rewrite took the request
// first. `/user/:path*` once took every `/user/batch-files` request that way, so
// that handler never ran: Next copied each upload into memory through the
// rewrite and cut it off at `middlewareClientMaxBodySize`.
//
// Executes the real config and compiles each rule with the function Next's
// router uses, against sample paths for every `route.ts` under `src/app`, so a
// handler added later is covered without editing this file.

import { readdirSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

import { describe, expect, it } from 'vitest';

// eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
const config = require('./next.config.js');
// eslint-disable-next-line @typescript-eslint/no-require-imports -- how `next start` compiles a rewrite.
const { buildCustomRoute } = require('next/dist/server/lib/router-utils/filesystem');

type Rewrite = { source: string; destination: string };

/** Every `route.ts` under src/app, as its route: `/v1/[[...path]]`. */
function routeHandlers(): string[] {
  const root = join(__dirname, 'src/app');
  const routes: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.isDirectory()) walk(join(dir, entry.name));
      else if (entry.name === 'route.ts') {
        // A `(group)` folder is not part of the URL.
        const segments = relative(root, dir)
          .split(sep)
          .filter((segment) => segment && !/^\(.*\)$/.test(segment));
        routes.push(`/${segments.join('/')}`);
      }
    }
  };
  walk(root);
  return routes;
}

/** Paths `route` serves: each dynamic segment filled in, an optional catch-all also left out. */
function samplePaths(route: string): string[] {
  let paths = [''];
  for (const segment of route.split('/').filter(Boolean)) {
    if (/^\[\[\.\.\..+\]\]$/.test(segment)) {
      paths = paths.flatMap((path) => [path, `${path}/a`, `${path}/a/b`]);
    } else if (/^\[\.\.\..+\]$/.test(segment)) {
      paths = paths.flatMap((path) => [`${path}/a`, `${path}/a/b`]);
    } else if (/^\[.+\]$/.test(segment)) {
      paths = paths.map((path) => `${path}/a`);
    } else {
      paths = paths.map((path) => `${path}/${segment}`);
    }
  }
  return paths.map((path) => path || '/');
}

async function rewriteTaking(path: string): Promise<Rewrite | undefined> {
  const { beforeFiles, afterFiles, fallback = [] } = await config.rewrites();
  return [...beforeFiles, ...afterFiles, ...fallback].find((rule: Rewrite) =>
    buildCustomRoute('rewrite', rule, '', false).match(path),
  );
}

describe('the rewrites and the route handlers', () => {
  it('finds the gateway handlers this test is meant to protect', () => {
    expect(routeHandlers()).toEqual(
      expect.arrayContaining(['/v1/[[...path]]', '/user/batch-files/[[...path]]']),
    );
  });

  it('leave every path a route handler serves to that handler', async () => {
    for (const route of routeHandlers()) {
      for (const path of samplePaths(route)) {
        expect(await rewriteTaking(path), `${path} (${route})`).toBeUndefined();
      }
    }
  });

  it('send batch uploads to their streaming handler, not through a rewrite', async () => {
    for (const path of [
      '/user/batch-files',
      '/user/batch-files/file-1',
      '/user/batch-files/file-1/content',
    ]) {
      expect(await rewriteTaking(path), path).toBeUndefined();
    }
  });
});
