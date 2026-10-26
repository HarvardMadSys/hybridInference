// The one alert-history ingestion path reaches the gateway — and only it.
//
// Executes the real config, like next.config.agents.test.ts: what matters is
// the resolved `afterFiles` table, not how the file spells it.

import { afterEach, describe, expect, it } from 'vitest';

const BACKEND = 'http://backend:8000';

async function afterFiles() {
  process.env.BACKEND_INTERNAL_URL = BACKEND;
  const path = require.resolve('./next.config.js');
  delete require.cache[path];
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
  const config = require('./next.config.js');
  const rewrites = await config.rewrites();
  return rewrites.afterFiles as Array<{ source: string; destination: string }>;
}

describe('the alert-history rewrite', () => {
  const saved = { ...process.env };
  afterEach(() => {
    process.env = { ...saved };
  });

  it('forwards POST /internal/alert-history/events to the gateway, path unchanged', async () => {
    const rules = await afterFiles();
    expect(rules).toContainEqual({
      source: '/internal/alert-history/events',
      destination: `${BACKEND}/internal/alert-history/events`,
    });
  });

  it('does not open the /internal prefix or any other alert-history path', async () => {
    const rules = await afterFiles();
    const sources = rules.map((rule) => rule.source);
    expect(sources).not.toContain('/internal/:path*');
    expect(sources.filter((source) => source.includes('alert-history'))).toEqual([
      '/internal/alert-history/events',
    ]);
  });
});
