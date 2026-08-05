/**
 * The trailing-slash redirect lives here only because next.config.js turns
 * Next's own version off for the pgAdmin proxy. These tests pin both halves of
 * that bargain: the proxied path keeps its slash, everything else loses it.
 */
import { NextRequest } from 'next/server';
import { describe, expect, it } from 'vitest';

import { middleware } from './middleware';

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

  it('leaves paths that already have no trailing slash alone', () => {
    expect(get('/login').headers.get('location')).toBeNull();
    expect(get('/').headers.get('location')).toBeNull();
  });
});
