// What the rewrites let through, and what Next copies on the way.
//
// Two of Next's defaults broke API calls that the gateway itself would have
// answered, both as a bare HTTP 500 from the console:
//
// - `proxyTimeout`, 30s. A non-streaming call the gateway takes longer to
//   answer -- a long completion, a usage insights report -- was cut while the
//   gateway was still working on it. The gateway bounds every request itself
//   (REQUEST_TIMEOUT_SECONDS; STREAM_REQUEST_TIMEOUT_SECONDS, an hour, for
//   streams), so the console must outwait both rather than cut in first.
// - `middlewareClientMaxBodySize`, 10MB. Next keeps only that much of a body
//   it copies -- for a middleware, and for every rewrite -- and forwards it
//   under the original Content-Length, so the gateway waited for the rest
//   until the timeout.
//
// Raising the second to fit the API made it memory any anonymous caller could
// pin per request, and a few dozen stalled uploads ended the console. So no
// gateway path that takes a body is a rewrite any more: each is a route
// handler, which streams the body and copies nothing, and the limit is cut to
// what a page might be sent.

import { afterEach, describe, expect, it } from 'vitest';

// eslint-disable-next-line @typescript-eslint/no-require-imports -- how `next start` compiles a rewrite.
const { buildCustomRoute } = require('next/dist/server/lib/router-utils/filesystem');

type Rewrite = { source: string; destination: string };

function loadConfig(env: Record<string, string | undefined> = {}) {
  for (const [name, value] of Object.entries(env)) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  // CommonJS, and cached by path: drop it so each case re-reads the environment.
  const path = require.resolve('./next.config.js');
  delete require.cache[path];
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
  return require('./next.config.js');
}

const NEUTRAL = { AGENT_WEB_INTERNAL_URL: undefined, AGENT_CONTROL_PLANE_INTERNAL_URL: undefined };

describe('the rewrites', () => {
  const saved = { ...process.env };
  afterEach(() => {
    process.env = { ...saved };
  });

  it('outwait the gateway’s own limits instead of answering 500 at 30s', () => {
    expect(loadConfig(NEUTRAL).experimental.proxyTimeout).toBeGreaterThanOrEqual(60 * 60 * 1000);
  });

  // The gateway serves only GET on each of these. Anything that takes a body
  // belongs in a route handler (src/lib/server/gatewayProxy.ts), and a new
  // rewrite has to be added here on purpose.
  it('forward only the gateway’s public GET documents', async () => {
    const { beforeFiles, afterFiles, fallback = [] } = await loadConfig(NEUTRAL).rewrites();

    expect(beforeFiles).toEqual([]);
    expect(fallback).toEqual([]);
    expect(afterFiles.map((rule: Rewrite) => rule.source)).toEqual([
      '/health',
      '/site-updates',
      '/site-config',
      '/public-stats',
    ]);
  });

  it('take no gateway request that carries a body', async () => {
    const { afterFiles } = await loadConfig(NEUTRAL).rewrites();
    const routes = afterFiles.map((rule: Rewrite) => buildCustomRoute('rewrite', rule, '', false));

    for (const path of [
      '/v1/chat/completions',
      '/anthropic/v1/messages',
      '/auth/login',
      '/auth/setup/admin',
      '/user/api-keys',
      '/user/batch-files',
      '/admin/settings/restore',
      '/internal/agent-grants',
      '/internal/agent-grants/g1/renew',
      '/internal/alert-history/events',
      '/internal/playground/chat',
    ]) {
      expect(
        routes.some((route: { match: (path: string) => unknown }) => route.match(path)),
        path,
      ).toBe(false);
    }
  });
});

describe('the body Next copies', () => {
  const saved = { ...process.env };
  afterEach(() => {
    process.env = { ...saved };
  });

  // Each copy is held in this container's memory until its request ends -- two
  // of them for a page, which the middleware also sees -- so the limit is what
  // one anonymous caller can pin per request. Pages take no body, and the
  // rewrites above forward GETs; nothing reads a body this would cut short.
  it('is a few KiB, now that nothing that takes a body is copied', () => {
    const limit = loadConfig(NEUTRAL).experimental.middlewareClientMaxBodySize;

    expect(limit).toBeGreaterThan(0);
    expect(limit).toBeLessThanOrEqual(64 * 1024);
  });

  // An image built with the legacy `/agents` rewrites forwards the agent's
  // API bodies through them, as it always has.
  it('stays at 32MiB in a build with the legacy agent rewrites compiled in', () => {
    const config = loadConfig({
      AGENT_WEB_INTERNAL_URL: 'http://agent-web:3000',
      AGENT_CONTROL_PLANE_INTERNAL_URL: 'http://agent-control-plane:8000',
    });

    expect(config.experimental.middlewareClientMaxBodySize).toBe(32 * 1024 * 1024);
  });
});
