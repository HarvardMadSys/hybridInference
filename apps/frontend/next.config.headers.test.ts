// The security headers next.config.js adds, and the paths they skip.
//
// pgAdmin and the agent web app are other applications served under /pgadmin
// and /agents, with their own scripts, framing and headers; everything else is
// the console or the gateway behind it.

import { describe, expect, it } from 'vitest';

// eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
const config = require('./next.config.js');
// eslint-disable-next-line @typescript-eslint/no-require-imports -- the matcher Next applies `headers()` with.
const { getPathMatch } = require('next/dist/shared/lib/router/utils/path-match');

type HeaderRule = { source: string; headers: { key: string; value: string }[] };

async function headersFor(path: string): Promise<Record<string, string>> {
  const rules: HeaderRule[] = await config.headers();
  const applied: Record<string, string> = {};
  for (const rule of rules) {
    if (!getPathMatch(rule.source, { strict: true, removeUnnamedParams: true })(path)) continue;
    for (const { key, value } of rule.headers) applied[key.toLowerCase()] = value;
  }
  return applied;
}

describe('security headers', () => {
  it('reach the console’s pages and the gateway’s responses', async () => {
    for (const path of [
      '/',
      '/login',
      '/dashboard/admin/users',
      '/v1/chat/completions',
      '/auth/login',
    ]) {
      expect(await headersFor(path), path).toEqual({
        'x-content-type-options': 'nosniff',
        'x-frame-options': 'DENY',
        'referrer-policy': 'strict-origin-when-cross-origin',
      });
    }
  });

  it('leave pgAdmin and the agent web app to set their own', async () => {
    for (const path of ['/pgadmin', '/pgadmin/browser/', '/agents', '/agents/api/v1/jobs']) {
      expect(await headersFor(path), path).toEqual({});
    }
  });
});
