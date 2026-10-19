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
// - `middlewareClientMaxBodySize`, 10MB. With a middleware present Next keeps
//   only that much of a body and forwards it under the original
//   Content-Length, so the gateway waited for the rest until the timeout.

import { describe, expect, it } from 'vitest';

// eslint-disable-next-line @typescript-eslint/no-require-imports -- the real, executed config.
const config = require('./next.config.js');

describe('the gateway rewrites', () => {
  it('outwait the gateway’s own limits instead of answering 500 at 30s', () => {
    expect(config.experimental.proxyTimeout).toBeGreaterThanOrEqual(60 * 60 * 1000);
  });

  it('forward a body as large as the edge accepts, not just its first 10MB', () => {
    expect(config.experimental.middlewareClientMaxBodySize).toBeGreaterThanOrEqual(
      100 * 1024 * 1024,
    );
  });
});
