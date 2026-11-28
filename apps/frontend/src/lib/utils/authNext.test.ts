// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  AUTH_NEXT_LIFETIME_MS,
  AUTH_NEXT_STORAGE_KEY,
  forgetAuthNext,
  loginHref,
  recallAuthNext,
  rememberAuthNext,
  signupHref,
} from './authNext';

const CONSENT = '/authorize/key?callback_url=https%3A%2F%2Fagent.example.org%2Fcb&code_challenge=x';

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('remembering where a sign-up leads', () => {
  it('gives back the internal path it was given', () => {
    rememberAuthNext(CONSENT);

    expect(recallAuthNext()).toBe(CONSENT);
  });

  it.each([
    'https://evil.test/phish',
    '//evil.test/phish',
    '/\\evil.test/phish',
    '/\n/evil.test/phish',
    '',
    null,
    undefined,
  ])('keeps nothing for %j', (path) => {
    rememberAuthNext(path);

    expect(window.localStorage.getItem(AUTH_NEXT_STORAGE_KEY)).toBeNull();
    expect(recallAuthNext()).toBeNull();
  });

  it('forgets a destination older than a verification link', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-28T00:00:00Z'));
    rememberAuthNext(CONSENT);

    vi.setSystemTime(Date.now() + AUTH_NEXT_LIFETIME_MS + 1);

    expect(recallAuthNext()).toBeNull();
    expect(window.localStorage.getItem(AUTH_NEXT_STORAGE_KEY)).toBeNull();
  });

  it('does not trust what storage holds: an off-site path is refused on the way out', () => {
    window.localStorage.setItem(
      AUTH_NEXT_STORAGE_KEY,
      JSON.stringify({ path: '//evil.test/phish', at: Date.now() }),
    );

    expect(recallAuthNext()).toBeNull();
  });

  it('ignores a value it cannot read', () => {
    window.localStorage.setItem(AUTH_NEXT_STORAGE_KEY, 'not json');

    expect(recallAuthNext()).toBeNull();
  });

  it('carries on when storage refuses', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError');
    });

    expect(() => rememberAuthNext(CONSENT)).not.toThrow();
    expect(recallAuthNext()).toBeNull();
  });

  it('can be forgotten', () => {
    rememberAuthNext(CONSENT);
    forgetAuthNext();

    expect(recallAuthNext()).toBeNull();
  });
});

describe('links that carry next', () => {
  it('carries an internal path, encoded', () => {
    expect(loginHref(CONSENT)).toBe(`/login?next=${encodeURIComponent(CONSENT)}`);
    expect(signupHref(CONSENT)).toBe(`/signup?next=${encodeURIComponent(CONSENT)}`);
  });

  it('stays bare without one, or for an off-site one', () => {
    expect(loginHref(null)).toBe('/login');
    expect(signupHref(undefined)).toBe('/signup');
    expect(loginHref('https://evil.test/phish')).toBe('/login');
    expect(signupHref('//evil.test')).toBe('/signup');
  });
});
