import { describe, expect, it } from 'vitest';

import { internalPathOr } from './navigation';

describe('internalPathOr', () => {
  it('accepts an app-internal path, with query intact', () => {
    expect(internalPathOr('/dashboard', '/authorize?client_id=cloud-agent&state=x')).toBe(
      '/authorize?client_id=cloud-agent&state=x',
    );
  });

  it('falls back when the value is missing or empty', () => {
    expect(internalPathOr('/dashboard', null)).toBe('/dashboard');
    expect(internalPathOr('/dashboard', undefined)).toBe('/dashboard');
    expect(internalPathOr('/dashboard', '')).toBe('/dashboard');
  });

  // Each of these is a way of writing "another site" that still begins the
  // string plausibly. The protocol-relative pair is the classic open-redirect:
  // the browser treats the second slash as the start of an authority.
  it.each([
    'https://evil.test/phish',
    '//evil.test/phish',
    '/\\evil.test/phish',
    'javascript:alert(1)',
    'dashboard', // relative — resolves against the current path, not the app root
  ])('falls back for %s', (raw) => {
    expect(internalPathOr('/dashboard', raw)).toBe('/dashboard');
  });
});
