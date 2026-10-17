// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AuthUser } from './AuthProvider';
import type { Locale } from '@/lib/i18n/locales';

/**
 * The browser's language choice against the signed-in account's: a new browser
 * takes the account's, a choice made signed out is carried into the account,
 * a choice belongs to the account it was made in, a failed store is retried,
 * and a late answer never undoes something newer.
 */

const refresh = vi.fn();
const nav = vi.hoisted(() => ({ pathname: '/dashboard' }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ refresh }),
  usePathname: () => nav.pathname,
}));

let user: AuthUser | null;
vi.mock('./AuthProvider', () => ({
  useAuth: () => ({ state: { isAuthenticated: user !== null, loading: false, user } }),
}));

const updateLocalePreference = vi.fn();
vi.mock('@/lib/api/user', () => ({
  updateLocalePreference: (...args: unknown[]) => updateLocalePreference(...args),
}));

import { LocaleAccountSync } from './LocaleAccountSync';
import { LocaleProvider } from './LocaleProvider';

const T0 = Date.parse('2026-10-01T00:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString();

function cookie(): string {
  const match = document.cookie.match(/(?:^|; )hi_locale=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : '';
}

function setCookie(value: string) {
  document.cookie = `hi_locale=${encodeURIComponent(value)}; Path=/`;
}

/** A fresh user object, as each profile load hands over. */
function signIn(id: string, locale: AuthUser['locale']) {
  user = { id, email: `${id}@example.test`, role: 'free', is_admin: false, locale };
}

function tree(current: Locale) {
  return (
    <LocaleProvider locale={current} catalog={undefined} moduleLocale="">
      <LocaleAccountSync />
    </LocaleProvider>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

let warn: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  nav.pathname = '/dashboard';
  refresh.mockReset();
  updateLocalePreference.mockReset();
  document.cookie = 'hi_locale=; Path=/; Max-Age=0';
  user = null;
  warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
});
afterEach(() => {
  cleanup();
  warn.mockRestore();
});

describe('LocaleAccountSync', () => {
  it('does nothing while signed out', () => {
    setCookie(`zh-CN|${T0}|a`);
    render(tree('zh-CN'));
    expect(updateLocalePreference).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("shows a new browser the account's choice", () => {
    signIn('a', { value: 'zh-CN', chosen_at: iso(T0) });
    render(tree('en'));
    expect(cookie()).toBe(`zh-CN|${T0}|s|a`);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('carries a choice made signed out into the account, which then owns it', async () => {
    setCookie(`zh-CN|${T0 + 5_000}|a`);
    signIn('a', { value: 'en', chosen_at: iso(T0) });
    updateLocalePreference.mockResolvedValue({ value: 'zh-CN', chosen_at: iso(T0 + 5_000) });

    render(tree('zh-CN'));

    expect(updateLocalePreference).toHaveBeenCalledWith('zh-CN', T0 + 5_000);
    await waitFor(() => expect(cookie()).toBe(`zh-CN|${T0 + 5_000}|s|a`));
    expect(refresh).not.toHaveBeenCalled();
  });

  it('shows the choice the account kept when another browser chose more recently', async () => {
    setCookie(`zh-CN|${T0}|a`);
    signIn('a', { value: 'zh-CN', chosen_at: iso(T0 - 1) });
    updateLocalePreference.mockResolvedValue({ value: 'en', chosen_at: iso(T0 + 1) });

    render(tree('zh-CN'));

    await waitFor(() => expect(cookie()).toBe(`en|${T0 + 1}|s|a`));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does not write account A's failed choice into account B", () => {
    // A chose Chinese and storing it failed; the reader switched to B (English).
    setCookie(`zh-CN|${T0 + 60_000}|p|a`);
    signIn('b', { value: 'en', chosen_at: iso(T0) });

    render(tree('zh-CN'));

    expect(updateLocalePreference).not.toHaveBeenCalled();
    expect(cookie()).toBe(`en|${T0}|s|b`);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("leaves account A's failed choice alone in an account B that never chose", () => {
    setCookie(`zh-CN|${T0}|p|a`);
    signIn('b', null);

    render(tree('zh-CN'));

    expect(updateLocalePreference).not.toHaveBeenCalled();
    expect(cookie()).toBe(`zh-CN|${T0}|p|a`);
  });

  it("shows a switched-to account its own choice, not the previous account's stored one", () => {
    setCookie(`zh-CN|${T0 + 60_000}|s|a`);
    signIn('b', { value: 'en', chosen_at: iso(T0) });

    render(tree('zh-CN'));

    expect(updateLocalePreference).not.toHaveBeenCalled();
    expect(cookie()).toBe(`en|${T0}|s|b`);
  });

  it('retries a failed store on the next profile load, and when the browser is back online', async () => {
    setCookie(`zh-CN|${T0}|p|a`);
    signIn('a', null);
    updateLocalePreference.mockRejectedValueOnce(new Error('offline'));

    const { rerender } = render(tree('zh-CN'));
    await waitFor(() => expect(warn).toHaveBeenCalledTimes(1));
    expect(cookie()).toBe(`zh-CN|${T0}|p|a`);

    // The profile is loaded again: same values, a new object.
    updateLocalePreference.mockRejectedValueOnce(new Error('still offline'));
    signIn('a', null);
    rerender(tree('zh-CN'));
    await waitFor(() => expect(warn).toHaveBeenCalledTimes(2));
    expect(updateLocalePreference).toHaveBeenCalledTimes(2);

    updateLocalePreference.mockResolvedValueOnce({ value: 'zh-CN', chosen_at: iso(T0) });
    act(() => {
      window.dispatchEvent(new Event('online'));
    });
    await waitFor(() => expect(cookie()).toBe(`zh-CN|${T0}|s|a`));
    expect(updateLocalePreference).toHaveBeenCalledTimes(3);
  });

  it('sends one request per choice while it is in flight', () => {
    setCookie(`zh-CN|${T0}|a`);
    signIn('a', null);
    updateLocalePreference.mockReturnValue(new Promise(() => undefined));

    const { rerender } = render(tree('zh-CN'));
    signIn('a', null);
    rerender(tree('zh-CN'));
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });

    expect(updateLocalePreference).toHaveBeenCalledTimes(1);
  });

  it('drops an answer that arrives after the reader switched accounts', async () => {
    setCookie(`zh-CN|${T0}|p|a`);
    signIn('a', null);
    const answer = deferred<unknown>();
    updateLocalePreference.mockReturnValue(answer.promise);

    const { rerender } = render(tree('zh-CN'));
    // Switch to B, which takes its own English.
    signIn('b', { value: 'en', chosen_at: iso(T0 - 1) });
    rerender(tree('zh-CN'));
    expect(cookie()).toBe(`en|${T0 - 1}|s|b`);

    await act(async () => {
      answer.resolve({ value: 'zh-CN', chosen_at: iso(T0) });
      await answer.promise;
    });

    expect(cookie()).toBe(`en|${T0 - 1}|s|b`);
  });

  it('drops an answer for a choice the reader has since replaced', async () => {
    setCookie(`zh-CN|${T0}|a`);
    signIn('a', null);
    const answer = deferred<unknown>();
    updateLocalePreference.mockReturnValue(answer.promise);

    render(tree('zh-CN'));
    setCookie(`en|${T0 + 1}|p|a`);

    await act(async () => {
      answer.resolve({ value: 'zh-CN', chosen_at: iso(T0) });
      await answer.promise;
    });

    expect(cookie()).toBe(`en|${T0 + 1}|p|a`);
  });

  it('asks again after a navigation when the page is still in another language', () => {
    // Signing in adopted Chinese, but the sign-in page navigated away before
    // the refresh landed: the dashboard arrived in English.
    setCookie(`zh-CN|${T0}|s|a`);
    signIn('a', { value: 'zh-CN', chosen_at: iso(T0) });
    const { rerender } = render(tree('en'));
    refresh.mockClear();

    nav.pathname = '/dashboard/settings';
    rerender(tree('en'));
    expect(refresh).toHaveBeenCalledTimes(1);

    // Once the page is in the chosen language, navigation asks for nothing.
    refresh.mockClear();
    nav.pathname = '/dashboard';
    rerender(tree('zh-CN'));
    expect(refresh).not.toHaveBeenCalled();
  });
});
