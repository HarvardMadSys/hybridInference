// What the gateway rewrites let through.
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
//   until the timeout. The API's own paths are route handlers outside the
//   middleware now (src/middleware.ts), which Next never copies; what remains
//   are the rewrites, and they must pass whatever the gateway would accept.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

// eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
const config = require('./next.config.js');

describe('the gateway rewrites', () => {
  it('outwait the gateway’s own limits instead of answering 500 at 30s', () => {
    expect(config.experimental.proxyTimeout).toBeGreaterThanOrEqual(60 * 60 * 1000);
  });

  // Each copy is held in this container's memory until its request ends, so
  // the limit is also what one anonymous caller can pin per request: it
  // should cover what the gateway accepts and nothing beyond. A body over the
  // gateway's limit is refused on its declared length, before it is read.
  it('copy as much of a body as the gateway accepts by default, and no more', () => {
    const bodyLimit = readFileSync(
      join(__dirname, '../backend/serving/servers/middleware/body_limit.py'),
      'utf8',
    );
    const megabytes = Number(/^DEFAULT_MAX_BODY_MB = (\d+)$/m.exec(bodyLimit)?.[1]);

    expect(megabytes).toBeGreaterThan(0);
    expect(config.experimental.middlewareClientMaxBodySize).toBe(megabytes * 1024 * 1024);
  });
});
