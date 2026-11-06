import { describe, expect, it } from 'vitest';

import { consolePagePolicy, makeNonce } from './contentSecurityPolicy';

function directives(policy: string): Record<string, string[]> {
  return Object.fromEntries(
    policy.split(';').map((part) => {
      const [name, ...values] = part.trim().split(/\s+/);
      return [name, values];
    }),
  );
}

const production = { apiBase: '', development: false };

describe('consolePagePolicy', () => {
  it('runs only scripts that carry the nonce, and what they load', () => {
    const script = directives(consolePagePolicy('abc', production))['script-src'];

    expect(script).toContain("'nonce-abc'");
    expect(script).toContain("'strict-dynamic'");
    expect(script).not.toContain("'unsafe-inline'");
    expect(script).not.toContain("'unsafe-eval'");
    expect(script).not.toContain('https:');
  });

  it('refuses plugins, a foreign <base>, foreign form targets and framing', () => {
    const policy = directives(consolePagePolicy('abc', production));

    expect(policy['object-src']).toEqual(["'none'"]);
    expect(policy['base-uri']).toEqual(["'self'"]);
    expect(policy['form-action']).toEqual(["'self'"]);
    expect(policy['frame-ancestors']).toEqual(["'none'"]);
    expect(policy['default-src']).toEqual(["'self'"]);
  });

  it('loads images over HTTPS or from the page itself, never over plain HTTP', () => {
    const images = directives(consolePagePolicy('abc', production))['img-src'];

    expect(images).toEqual(["'self'", 'data:', 'blob:', 'https:']);
  });

  it('lets the browser reach a gateway on another origin', () => {
    const connect = (apiBase: string) =>
      directives(consolePagePolicy('abc', { apiBase, development: false }))['connect-src'];

    expect(connect('http://localhost:8080')).toContain('http://localhost:8080');
    expect(connect('https://gateway.example.com/base')).toContain('https://gateway.example.com');
    expect(connect('')).toEqual(expect.not.arrayContaining([expect.stringMatching(/^http:/)]));
    expect(connect('not a url')).toContain("'self'");
  });

  it('allows what `next dev` needs only in development', () => {
    const dev = directives(consolePagePolicy('abc', { apiBase: '', development: true }));

    expect(dev['script-src']).toContain("'unsafe-eval'");
    expect(dev['connect-src']).toContain('ws:');
  });
});

describe('makeNonce', () => {
  it('is new for every response, and long enough not to guess', () => {
    const nonces = new Set(Array.from({ length: 50 }, makeNonce));

    expect(nonces.size).toBe(50);
    for (const nonce of nonces) expect(atob(nonce)).toHaveLength(16);
  });
});
