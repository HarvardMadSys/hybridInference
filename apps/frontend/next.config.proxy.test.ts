// How long the gateway rewrites wait for the gateway.
//
// Next's default `proxyTimeout` is 30s. A non-streaming call the gateway takes
// longer to answer -- a long completion, a usage insights report -- then came
// back as a bare HTTP 500 from the console while the gateway was still working
// on it. The gateway bounds every request itself (REQUEST_TIMEOUT_SECONDS,
// 120s by default; STREAM_REQUEST_TIMEOUT_SECONDS, an hour, for streams), so
// the console must outwait both rather than cut in first.

import { describe, expect, it } from 'vitest';

describe('the gateway rewrites', () => {
  it('outwait the gateway’s own limits instead of answering 500 at 30s', () => {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
    const config = require('./next.config.js');
    expect(config.experimental.proxyTimeout).toBeGreaterThanOrEqual(60 * 60 * 1000);
  });
});
