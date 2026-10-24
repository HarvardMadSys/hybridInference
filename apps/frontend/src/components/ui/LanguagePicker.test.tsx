// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Locale } from '@/lib/i18n/locales';

const refresh = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ refresh }) }));

const refreshUser = vi.fn();
/** The signed-in account's id, or `null` signed out. */
let signedInAs: string | null = null;
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: { isAuthenticated: signedInAs !== null, user: signedInAs ? { id: signedInAs } : null },
    refreshUser,
  }),
}));

const updateLocalePreference = vi.fn();
vi.mock('@/lib/api/user', () => ({
  updateLocalePreference: (...args: unknown[]) => updateLocalePreference(...args),
}));

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LanguagePicker } from './LanguagePicker';

const NOW = Date.parse('2026-10-17T08:00:00Z');

function cookie(): string {
  const match = document.cookie.match(/(?:^|; )hi_locale=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : '';
}

function renderIn(locale: Locale, ui: React.ReactNode) {
  return render(
    <LocaleProvider locale={locale} catalog={undefined} moduleLocale="">
      {ui}
    </LocaleProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ['Date'] });
  refresh.mockReset();
  refreshUser.mockReset().mockResolvedValue(undefined);
  updateLocalePreference.mockReset();
  document.cookie = 'hi_locale=; Path=/; Max-Age=0';
  signedInAs = null;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('LanguagePicker', () => {
  it("names every language in that language's own name and marks the current one", () => {
    renderIn('zh-CN', <LanguagePicker />);
    expect(screen.getByRole('radio', { name: '中文' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'English' })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: 'English' }).closest('label')).toHaveAttribute(
      'lang',
      'en',
    );
    expect(screen.getByRole('radio', { name: '中文' }).closest('label')).toHaveAttribute(
      'lang',
      'zh-CN',
    );
  });

  it('keeps a signed-out choice in the browser, owned by nobody, and re-renders the route', () => {
    renderIn('zh-CN', <LanguagePicker />);
    fireEvent.click(screen.getByRole('radio', { name: 'English' }));

    expect(cookie()).toBe(`en|${NOW}|a`);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(updateLocalePreference).not.toHaveBeenCalled();
  });

  it("stores a signed-in choice in the account and takes the server's time", async () => {
    signedInAs = 'a';
    updateLocalePreference.mockResolvedValue({
      value: 'zh-CN',
      chosen_at: '2026-10-17T08:00:01.000Z',
    });

    renderIn('en', <LanguagePicker />);
    fireEvent.click(screen.getByRole('radio', { name: '中文' }));

    expect(updateLocalePreference).toHaveBeenCalledWith('zh-CN');
    expect(cookie()).toBe(`zh-CN|${NOW}|p|a`);
    await waitFor(() => expect(cookie()).toBe(`zh-CN|${NOW + 1000}|s|a`));
    expect(refreshUser).toHaveBeenCalledTimes(1);
  });

  it("keeps a signed-in choice the account could not store as that account's, unstored", async () => {
    signedInAs = 'a';
    updateLocalePreference.mockRejectedValue(new Error('offline'));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    renderIn('en', <LanguagePicker />);
    fireEvent.click(screen.getByRole('radio', { name: '中文' }));

    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(cookie()).toBe(`zh-CN|${NOW}|p|a`);
    warn.mockRestore();
  });

  it("drops the old account's answer once the browser has moved to another account", async () => {
    signedInAs = 'a';
    let answer!: (value: unknown) => void;
    updateLocalePreference.mockReturnValue(new Promise((resolve) => (answer = resolve)));

    const { rerender } = renderIn('en', <LanguagePicker />);
    fireEvent.click(screen.getByRole('radio', { name: '中文' }));

    // The reader switches to B, which adopts its own English.
    signedInAs = 'b';
    document.cookie = `hi_locale=${encodeURIComponent(`en|${NOW - 1}|s|b`)}; Path=/`;
    rerender(
      <LocaleProvider locale="en" catalog={undefined} moduleLocale="">
        <LanguagePicker />
      </LocaleProvider>,
    );

    await act(async () => {
      answer({ value: 'zh-CN', chosen_at: '2026-10-17T08:00:00.000Z' });
    });

    expect(cookie()).toBe(`en|${NOW - 1}|s|b`);
    expect(refreshUser).not.toHaveBeenCalled();
  });

  it('drops the answer to an earlier choice once the reader has chosen again', async () => {
    signedInAs = 'a';
    let answer!: (value: unknown) => void;
    updateLocalePreference.mockReturnValue(new Promise((resolve) => (answer = resolve)));

    renderIn('en', <LanguagePicker />);
    fireEvent.click(screen.getByRole('radio', { name: '中文' }));
    // Meanwhile the reader chose English — from the account menu, or in another tab.
    document.cookie = `hi_locale=${encodeURIComponent(`en|${NOW + 5_000}|p|a`)}; Path=/`;

    await act(async () => {
      answer({ value: 'zh-CN', chosen_at: '2026-10-17T08:00:00.000Z' });
    });

    expect(cookie()).toBe(`en|${NOW + 5_000}|p|a`);
    expect(refreshUser).not.toHaveBeenCalled();
  });
});
