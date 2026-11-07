// The one alert-history ingestion path reaches the gateway — and only it.
//
// It is published by a route file for exactly that path
// (src/app/internal/alert-history/events/route.ts), not by a prefix: the
// endpoint admits only a scoped writer credential and answers 404 while alert
// history is off, so relaying it publishes nothing a deployment has not
// configured. Reads stay under /admin. That the file relays to the gateway is
// src/lib/server/gatewayRoutes.test.ts's to check; this one checks that nothing
// else under the prefix is published.

import { existsSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';

import { describe, expect, it } from 'vitest';

const INTERNAL = join(__dirname, 'src/app/internal');

/** Every route file under `dir`, relative to src/app/internal. */
function routeFiles(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return routeFiles(path);
    return entry.name === 'route.ts' ? [relative(INTERNAL, path)] : [];
  });
}

async function rewriteSources(): Promise<string[]> {
  const path = require.resolve('./next.config.js');
  delete require.cache[path];
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
  const { beforeFiles, afterFiles, fallback = [] } = await require('./next.config.js').rewrites();
  return [...beforeFiles, ...afterFiles, ...fallback].map(
    (rule: { source: string }) => rule.source,
  );
}

describe('the alert-history ingestion path', () => {
  it('is published by a route file for POST /internal/alert-history/events alone', () => {
    expect(routeFiles(join(INTERNAL, 'alert-history'))).toEqual([
      join('alert-history', 'events', 'route.ts'),
    ]);
  });

  it('does not open the /internal prefix or any other alert-history path', async () => {
    // A catch-all directly under /internal would publish every route the
    // gateway adds there.
    const catchAll = readdirSync(INTERNAL).filter((name) => name.startsWith('['));
    expect(catchAll).toEqual([]);

    const sources = await rewriteSources();
    expect(sources.filter((source) => source.startsWith('/internal'))).toEqual([]);
  });
});
