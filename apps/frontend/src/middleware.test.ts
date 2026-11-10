/**
 * The trailing-slash redirect lives here only because next.config.js turns
 * Next's own version off for the pgAdmin and agent proxies. These tests pin
 * both halves of that bargain: proxied paths keep their slash, everything else
 * loses it.
 */
import { NextRequest } from 'next/server';
import { describe, expect, it } from 'vitest';

import { config, middleware } from './middleware';

// eslint-disable-next-line @typescript-eslint/no-require-imports -- the function Next's build compiles matchers with.
const { getMiddlewareMatchers } = require('next/dist/build/analysis/get-page-static-info');

function get(path: string) {
  return middleware(new NextRequest(`https://freeinference.org${path}`));
}

describe('trailing-slash middleware', () => {
  it('redirects a console page to its slashless form', () => {
    const response = get('/login/');

    expect(response.status).toBe(308);
    expect(response.headers.get('location')).toBe('https://freeinference.org/login');
  });

  it('keeps the query string when it redirects', () => {
    const response = get('/dashboard/?tab=keys');

    expect(response.headers.get('location')).toBe('https://freeinference.org/dashboard?tab=keys');
  });

  it('leaves the pgAdmin proxy alone, slash and all', () => {
    // Flask adds this slash back if it is stripped, and Next would strip it
    // again — the two would bounce the request between them indefinitely.
    for (const path of ['/pgadmin', '/pgadmin/', '/pgadmin/browser/', '/pgadmin/static/js/']) {
      const response = get(path);
      expect(response.headers.get('location'), path).toBeNull();
    }
  });

  it('leaves the agent proxy alone, slash and all', () => {
    // The standalone web app owns /agents and may canonicalize its own root to
    // /agents/. Stripping that slash here would create a redirect loop.
    for (const path of ['/agents', '/agents/', '/agents/jobs/', '/agents/api/v1/jobs/']) {
      const response = get(path);
      expect(response.headers.get('location'), path).toBeNull();
    }
  });

  it('leaves paths that already have no trailing slash alone', () => {
    expect(get('/login').headers.get('location')).toBeNull();
    expect(get('/').headers.get('location')).toBeNull();
  });
});

describe('which requests reach the middleware', () => {
  // Compiled by the same function `next build` uses, so this is Next's routing
  // rather than a reading of the pattern.
  const compiled: RegExp[] = getMiddlewareMatchers(config.matcher, {}).map(
    (matcher: { regexp: string }) => new RegExp(matcher.regexp),
  );
  const reaches = (path: string) => compiled.some((matcher) => matcher.test(path));

  it('takes in every console page', () => {
    for (const path of [
      '/',
      '/login',
      '/login/',
      '/dashboard/admin/users',
      '/authorize',
      '/stats/',
      '/verify-email',
    ]) {
      expect(reaches(path), path).toBe(true);
    }
  });

  // Next copies the body of every request a middleware sees and holds the copy
  // in memory, so the API's requests must not be among them.
  it('leaves the gateway and the proxied apps alone, so their bodies are never copied', () => {
    for (const path of [
      '/v1',
      '/v1/chat/completions',
      '/anthropic/v1/messages',
      '/auth/login',
      '/user/batch-files',
      '/admin/config',
      '/internal/playground/v1/chat/completions',
      '/internal/verify-admin',
      '/health',
      '/site-config',
      '/site-updates',
      '/public-stats',
      '/site-assets/logo.svg',
      '/pgadmin',
      '/pgadmin/browser/',
      '/agents',
      '/agents/api/v1/jobs/',
      '/_next/static/chunks/main.js',
      '/favicon.ico',
    ]) {
      expect(reaches(path), path).toBe(false);
    }
  });

  // A path matched only before Next strips its slash is answered with an
  // empty 200, so these get their redirect from next.config.js instead.
  it('leaves a gateway path that ends in a slash to next.config.js redirects', () => {
    for (const path of ['/v1/', '/v1/models/', '/auth/login/', '/health/']) {
      expect(reaches(path), path).toBe(false);
      expect(reaches(path.replace(/\/$/, '')), path).toBe(false);
    }
  });
});

describe('the content security policy', () => {
  function policyOf(response: Response): string {
    return response.headers.get('content-security-policy') ?? '';
  }

  // Next reads the policy, and the layout the nonce, from the request headers
  // the middleware hands on; NextResponse carries them as override headers.
  function forwarded(response: Response, name: string): string | null {
    return response.headers.get(`x-middleware-request-${name}`);
  }

  it('gives a page a policy whose script nonce reaches the renderer', () => {
    const response = get('/dashboard');
    const nonce = forwarded(response, 'x-nonce');

    expect(nonce).toBeTruthy();
    expect(policyOf(response)).toContain(`'nonce-${nonce}'`);
    expect(forwarded(response, 'content-security-policy')).toBe(policyOf(response));
  });

  it('makes a new nonce for every response', () => {
    const first = forwarded(get('/login'), 'x-nonce');
    const second = forwarded(get('/login'), 'x-nonce');

    expect(first).not.toBe(second);
  });

  it('replaces a nonce the caller sent', () => {
    const response = middleware(
      new NextRequest('https://freeinference.org/', { headers: { 'x-nonce': 'chosen' } }),
    );

    expect(forwarded(response, 'x-nonce')).not.toBe('chosen');
    expect(policyOf(response)).not.toContain('chosen');
  });

  it('leaves the proxied applications to set their own', () => {
    for (const path of ['/pgadmin/browser/', '/agents/jobs']) {
      expect(get(path).headers.get('content-security-policy'), path).toBeNull();
    }
  });
});
