// The trailing-slash redirect for the gateway's paths, which src/middleware.ts
// gives the console's pages but must not see the gateway's requests for.

import { describe, expect, it } from 'vitest';

// eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
const config = require('./next.config.js');
// eslint-disable-next-line @typescript-eslint/no-require-imports -- how `next start` compiles a redirect.
const { buildCustomRoute } = require('next/dist/server/lib/router-utils/filesystem');
// eslint-disable-next-line @typescript-eslint/no-require-imports -- how it fills in the destination.
const { prepareDestination } = require('next/dist/shared/lib/router/utils/prepare-destination');

type Redirect = { source: string; destination: string; permanent: boolean };

async function redirectFor(path: string): Promise<{ to: string; permanent: boolean } | null> {
  const rules: Redirect[] = await config.redirects();
  for (const rule of rules) {
    const route = buildCustomRoute('redirect', rule, '', false);
    const params = route.match(path);
    if (!params) continue;
    const { newUrl } = prepareDestination({
      appendParamsToQuery: false,
      destination: route.destination,
      params,
      query: {},
    });
    return { to: newUrl, permanent: rule.permanent };
  }
  return null;
}

describe('gateway trailing-slash redirects', () => {
  it('send a gateway path ending in a slash to the same path without it', async () => {
    for (const [from, to] of [
      ['/v1/', '/v1'],
      ['/v1/models/', '/v1/models'],
      ['/v1/chat/completions/', '/v1/chat/completions'],
      ['/anthropic/v1/messages/', '/anthropic/v1/messages'],
      ['/auth/login/', '/auth/login'],
      ['/health/', '/health'],
    ]) {
      expect(await redirectFor(from), from).toEqual({ to, permanent: true });
    }
  });

  it('leave everything else alone', async () => {
    for (const path of ['/v1/models', '/auth/login', '/login/', '/pgadmin/', '/agents/jobs/']) {
      expect(await redirectFor(path), path).toBeNull();
    }
  });
});
