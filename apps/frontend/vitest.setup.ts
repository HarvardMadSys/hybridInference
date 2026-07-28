/**
 * No unit test may reach the network.
 *
 * A component that fetches on mount will do it for real if the test forgets to
 * mock the call — and the default API base is a live gateway, so the suite was
 * sending requests to it on every CI run, and would do the same from every
 * contributor's machine. It is also how the suite acquired its one flake: a
 * real request can resolve after Vitest has torn the environment down, and the
 * setState that follows reaches for a `window` that no longer exists, throwing
 * where no test can catch it.
 *
 * Failing loudly here turns "someone forgot a mock" from an intermittent,
 * environment-dependent crash into a named error at the call site.
 */
import { afterEach, beforeEach, vi } from 'vitest';

let blocked: string[] = [];

function blockedFetch(input: RequestInfo | URL): never {
  const url = typeof input === 'string' ? input : String((input as Request).url ?? input);
  blocked.push(url);
  throw new Error(
    `Unit tests must not reach the network, but something requested ${url}. ` +
      'Mock the API module for this test (vi.mock(\'@/lib/api/...\')).',
  );
}

beforeEach(() => {
  blocked = [];
  vi.stubGlobal('fetch', vi.fn(blockedFetch));
});

// Components catch their own fetch errors and render an error state, so
// throwing above is not enough on its own — a forgotten mock would still pass
// silently. Fail the test that made the call.
afterEach(() => {
  const attempted = blocked;
  blocked = [];
  if (attempted.length > 0) {
    throw new Error(
      `This test reached the network for: ${[...new Set(attempted)].join(', ')}. ` +
        'Mock the API module it uses.',
    );
  }
});
