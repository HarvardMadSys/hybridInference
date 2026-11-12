// The console's routing against the public path manifest, deploy/public-paths.json.
//
// The manifest is the one list of the public paths that are not the console's
// own pages. The console implements it in places that cannot read a file:
// route handlers under src/app, `rewrites()` and `redirects()` in
// next.config.js, and the middleware's `matcher`, a literal string because
// Next reads it at build time without running the module. Each is compared
// with the manifest here, so changing one without the others fails.
//
// The rewrites, the redirect and the matcher are written per first segment
// (`internal`, not `/internal/verify-admin`): that is as fine as they can tell
// paths apart. What decides which `/internal` paths are published are the
// route files, checked one by one in src/lib/server/gatewayRoutes.test.ts.

import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

import { describe, expect, it } from 'vitest';

import { config as middlewareConfig } from './src/middleware';
import {
  PUBLIC_PATHS,
  samplePaths,
  topSegment,
  type PublicPath,
} from './src/test-utils/publicPaths';

// eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
const config = require('./next.config.js');
// eslint-disable-next-line @typescript-eslint/no-require-imports -- how `next start` compiles a rule.
const { buildCustomRoute } = require('next/dist/server/lib/router-utils/filesystem');
// eslint-disable-next-line @typescript-eslint/no-require-imports -- how `next build` compiles a matcher.
const { getMiddlewareMatchers } = require('next/dist/build/analysis/get-page-static-info');

type Rule = { source: string; destination: string };

/** Next's own paths, which the middleware skips and the manifest does not list. */
const NEXT_INTERNALS = ['_next/static', '_next/image', 'favicon.ico'];

/**
 * The one manifest path the middleware still sees: the console's container
 * health check takes no body, so being matched costs it nothing, and its
 * trailing-slash redirect is the middleware's, like a page's.
 */
const MIDDLEWARE_SEES = new Set(['/healthz']);

/** Owners that keep their own trailing-slash semantics: the proxied apps. */
const OWN_SLASHES = new Set<PublicPath['owner']>(['cloud-agent', 'pgadmin']);

const GATEWAY = PUBLIC_PATHS.filter((entry) => entry.owner === 'gateway');
const BYPASSING_MIDDLEWARE = PUBLIC_PATHS.filter((entry) => !MIDDLEWARE_SEES.has(entry.path));

const sorted = (values: Iterable<string>) => [...new Set(values)].sort();

/** The segments in the matcher's `(?!(?:a|b|c)(?:/|$))` look-ahead. */
function matcherExclusions(): string[] {
  expect(middlewareConfig.matcher).toHaveLength(1);
  const found = /\(\?!\(\?:([^)]*)\)\(\?:\/\|\$\)\)/.exec(middlewareConfig.matcher[0]);
  if (!found) throw new Error(`Unexpected matcher: ${middlewareConfig.matcher[0]}`);
  return found[1].split('|').map((segment) => segment.replace(/\\(.)/g, '$1'));
}

/** Each route file that relays to the gateway, as its URL and its mount. */
function gatewayRouteFiles(): Array<{ route: string; mount: string }> {
  const root = join(__dirname, 'src/app');
  const found: Array<{ route: string; mount: string }> = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.name === 'route.ts') {
        const mount = /gatewayRoute\('([^']+)'\)/.exec(readFileSync(path, 'utf8'));
        if (mount)
          found.push({ route: `/${relative(root, dir).split(sep).join('/')}`, mount: mount[1] });
      }
    }
  };
  walk(root);
  return found;
}

/** The manifest entry a route file serves: `/v1/[[...path]]` is the prefix `/v1`. */
function entryOf(route: string): string {
  const prefix = route.replace(/\/\[\[\.\.\.[^\]]+\]\]$/, '');
  const path = prefix.replace(/\[([^\]]+)\]/g, ':$1');
  return `${prefix === route ? 'exact' : 'prefix'} ${path}`;
}

describe('the public path manifest', () => {
  it('lists each path once, with the fields the tests and the probe read', () => {
    expect(PUBLIC_PATHS.length).toBeGreaterThan(0);
    expect(sorted(PUBLIC_PATHS.map((entry) => entry.path))).toHaveLength(PUBLIC_PATHS.length);
    for (const entry of PUBLIC_PATHS) {
      expect(entry.path, entry.path).toMatch(/^(\/[a-z0-9-]+|\/:[A-Za-z]+)+$/);
      expect(['prefix', 'exact'], entry.path).toContain(entry.match);
      expect(['gateway', 'console', 'cloud-agent', 'pgadmin'], entry.path).toContain(entry.owner);
      expect(typeof entry.entry_split, entry.path).toBe('boolean');
    }
  });

  // An entry may send only whole gateway prefixes past the console. Widening
  // the split is a deliberate change: the spec's acceptance run comes with it.
  it('lets an entry split off /v1 and /anthropic, and nothing else', () => {
    const split = PUBLIC_PATHS.filter((entry) => entry.entry_split);
    expect(split.map(({ path, match, owner }) => ({ path, match, owner }))).toEqual([
      { path: '/v1', match: 'prefix', owner: 'gateway' },
      { path: '/anthropic', match: 'prefix', owner: 'gateway' },
    ]);
  });
});

describe('the console against the manifest', () => {
  it('serves every gateway path by exactly one route handler or one rewrite', async () => {
    const { beforeFiles, afterFiles } = await config.rewrites();
    // A neutral build compiles in no legacy agent rewrite (see next.config.js).
    expect(beforeFiles).toEqual([]);

    const handled = gatewayRouteFiles().map(({ route }) => entryOf(route));
    const rewritten = (afterFiles as Rule[]).map((rule) => `exact ${rule.source}`);
    expect(sorted(handled)).toHaveLength(handled.length);
    expect(sorted([...handled, ...rewritten])).toEqual(
      sorted(GATEWAY.map((entry) => `${entry.match} ${entry.path}`)),
    );
    expect(handled.filter((entry) => rewritten.includes(entry))).toEqual([]);
  });

  it('rewrites a gateway path to the same path on the gateway', async () => {
    const { afterFiles } = await config.rewrites();
    for (const rule of afterFiles as Rule[]) {
      const destination = new URL(rule.destination);
      expect(destination.pathname + destination.search, rule.source).toBe(rule.source);
    }
  });

  it('mounts each gateway route handler at its own path or above it', () => {
    for (const { route, mount } of gatewayRouteFiles()) {
      const path = entryOf(route).split(' ')[1];
      expect(path === mount || path.startsWith(`${mount}/`), `${route} (${mount})`).toBe(true);
    }
  });

  it('keeps out of the middleware exactly the manifest’s first segments', () => {
    expect(sorted(matcherExclusions())).toEqual(
      sorted([...NEXT_INTERNALS, ...BYPASSING_MIDDLEWARE.map((entry) => topSegment(entry.path))]),
    );
  });

  it('routes the manifest’s paths past the middleware, with or without a trailing slash', () => {
    const compiled: RegExp[] = getMiddlewareMatchers(middlewareConfig.matcher, {}).map(
      (matcher: { regexp: string }) => new RegExp(matcher.regexp),
    );
    const reaches = (path: string) => compiled.some((matcher) => matcher.test(path));
    for (const entry of PUBLIC_PATHS) {
      for (const path of samplePaths(entry)) {
        const expected = MIDDLEWARE_SEES.has(entry.path);
        expect(reaches(path), path).toBe(expected);
        expect(reaches(`${path}/`), `${path}/`).toBe(expected);
      }
    }
  });

  it('redirects a trailing slash away on every manifest segment the middleware does not see', async () => {
    const [rule, ...others] = (await config.redirects()) as Rule[];
    expect(others).toEqual([]);
    const segments = /^\/:prefix\(([^)]*)\)\/:path\*\/$/.exec(rule.source);
    if (!segments) throw new Error(`Unexpected redirect source: ${rule.source}`);

    expect(sorted(segments[1].split('|'))).toEqual(
      sorted(
        BYPASSING_MIDDLEWARE.filter((entry) => !OWN_SLASHES.has(entry.owner)).map((entry) =>
          topSegment(entry.path),
        ),
      ),
    );

    const route = buildCustomRoute('redirect', rule, '', false);
    for (const entry of BYPASSING_MIDDLEWARE) {
      for (const path of samplePaths(entry)) {
        expect(Boolean(route.match(`${path}/`)), `${path}/`).toBe(!OWN_SLASHES.has(entry.owner));
      }
    }
  });
});
