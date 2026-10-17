// @vitest-environment jsdom
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AuthUser } from './AuthProvider';
import type { Locale } from '@/lib/i18n/locales';

/**
 * The browser's language choice against the signed-in account's: a new browser
 * takes the account's, a choice made signed out is carried into the account,
 * and switching accounts shows each account its own.
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

function cookie(): string {
  const match = document.cookie.match(/(?:^|; )hi_locale=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : '';
}

function setCookie(value: string) {
  document.cookie = `hi_locale=${encodeURIComponent(value)}; Path=/`;
}

function signIn(locale: AuthUser['locale']) {
  user = { id: 'u1', email: 'u1@example.test', role: 'free', is_admin: false, locale };
}

function renderSync(current: Locale) {
  return render(
    <LocaleProvider locale={current} catalog={undefined} moduleLocale="">
      <LocaleAccountSync />
    </LocaleProvider>,
  );
}

beforeEach(() => {
  nav.pathname = '/dashboard';
  refresh.mockReset();
  updateLocalePreference.mockReset();
  document.cookie = 'hi_locale=; Path=/; Max-Age=0';
  user = null;
});
afterEach(cleanup);

describe('LocaleAccountSync', () => {
  it('does nothing while signed out', () => {
    setCookie(`zh-CN|${T0}|p`);
    renderSync('zh-CN');
    expect(updateLocalePreference).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("shows a new browser the account's choice", () => {
    signIn({ value: 'zh-CN', chosen_at: new Date(T0).toISOString() });
    renderSync('en');
    expect(cookie()).toBe(`zh-CN|${T0}|s`);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('carries a choice made signed out into the account', async () => {
    setCookie(`zh-CN|${T0 + 5_000}|p`);
    signIn({ value: 'en', chosen_at: new Date(T0).toISOString() });
    updateLocalePreference.mockResolvedValue({
      value: 'zh-CN',
      chosen_at: new Date(T0 + 5_000).toISOString(),
    });

    renderSync('zh-CN');

    expect(updateLocalePreference).toHaveBeenCalledWith('zh-CN', T0 + 5_000);
    await waitFor(() => expect(cookie()).toBe(`zh-CN|${T0 + 5_000}|s`));
    expect(refresh).not.toHaveBeenCalled();
  });

  it('shows the choice the account kept when another browser chose more recently', async () => {
    setCookie(`zh-CN|${T0}|p`);
    signIn({ value: 'zh-CN', chosen_at: new Date(T0 - 1).toISOString() });
    // Meanwhile another browser chose English; the server keeps the newer one.
    updateLocalePreference.mockResolvedValue({
      value: 'en',
      chosen_at: new Date(T0 + 1).toISOString(),
    });

    renderSync('zh-CN');

    await waitFor(() => expect(cookie()).toBe(`en|${T0 + 1}|s`));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('keeps a pending choice pending when the account cannot store it', async () => {
    setCookie(`zh-CN|${T0}|p`);
    signIn(null);
    updateLocalePreference.mockRejectedValue(new Error('offline'));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    renderSync('zh-CN');

    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(cookie()).toBe(`zh-CN|${T0}|p`);
    warn.mockRestore();
  });

  it("shows a switched-to account its own choice, not the previous account's", () => {
    // The previous account chose Chinese, more recently than this one chose
    // English; it was stored there, so it is not this account's to take.
    setCookie(`zh-CN|${T0 + 60_000}|s`);
    signIn({ value: 'en', chosen_at: new Date(T0).toISOString() });

    renderSync('zh-CN');

    expect(updateLocalePreference).not.toHaveBeenCalled();
    expect(cookie()).toBe(`en|${T0}|s`);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('leaves an account that never chose, and a browser with no choice, on the system language', () => {
    signIn(null);
    renderSync('zh-CN');
    expect(cookie()).toBe('');
    expect(updateLocalePreference).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it('asks again after a navigation when the page is still in another language', () => {
    // Signing in adopted Chinese, but the sign-in page navigated away before
    // the refresh landed: the dashboard arrived in English.
    setCookie(`zh-CN|${T0}|s`);
    signIn({ value: 'zh-CN', chosen_at: new Date(T0).toISOString() });
    const { rerender } = renderSync('en');
    refresh.mockClear();

    nav.pathname = '/dashboard/settings';
    rerender(
      <LocaleProvider locale="en" catalog={undefined} moduleLocale="">
        <LocaleAccountSync />
      </LocaleProvider>,
    );
    expect(refresh).toHaveBeenCalledTimes(1);

    // Once the page is in the chosen language, navigation asks for nothing.
    refresh.mockClear();
    nav.pathname = '/dashboard';
    rerender(
      <LocaleProvider locale="zh-CN" catalog={undefined} moduleLocale="">
        <LocaleAccountSync />
      </LocaleProvider>,
    );
    expect(refresh).not.toHaveBeenCalled();
  });
});
