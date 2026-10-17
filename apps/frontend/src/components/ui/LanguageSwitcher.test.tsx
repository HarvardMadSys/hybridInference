// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Locale } from '@/lib/i18n/locales';

const refresh = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ refresh }) }));

const refreshUser = vi.fn();
let signedIn = false;
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { isAuthenticated: signedIn }, refreshUser }),
}));

const updateLocalePreference = vi.fn();
vi.mock('@/lib/api/user', () => ({
  updateLocalePreference: (...args: unknown[]) => updateLocalePreference(...args),
}));

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LanguagePicker, LanguageSwitcher } from './LanguageSwitcher';

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
  signedIn = false;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('LanguageSwitcher', () => {
  it("offers the other language by that language's own name", () => {
    renderIn('en', <LanguageSwitcher />);
    const button = screen.getByRole('button', { name: '中文' });
    expect(button).toHaveAttribute('lang', 'zh-CN');
    expect(screen.queryByRole('button', { name: 'English' })).not.toBeInTheDocument();

    cleanup();
    renderIn('zh-CN', <LanguageSwitcher />);
    expect(screen.getByRole('button', { name: 'English' })).toHaveAttribute('lang', 'en');
  });

  it('keeps a signed-out choice in the browser, pending, and re-renders the route', () => {
    renderIn('en', <LanguageSwitcher />);
    fireEvent.click(screen.getByRole('button', { name: '中文' }));

    expect(cookie()).toBe(`zh-CN|${NOW}|p`);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(updateLocalePreference).not.toHaveBeenCalled();
  });

  it("stores a signed-in choice in the account and takes the server's time", async () => {
    signedIn = true;
    updateLocalePreference.mockResolvedValue({
      value: 'zh-CN',
      chosen_at: '2026-10-17T08:00:01.000Z',
    });

    renderIn('en', <LanguageSwitcher />);
    fireEvent.click(screen.getByRole('button', { name: '中文' }));

    expect(updateLocalePreference).toHaveBeenCalledWith('zh-CN');
    await waitFor(() => expect(cookie()).toBe(`zh-CN|${NOW + 1000}|s`));
    expect(refreshUser).toHaveBeenCalledTimes(1);
  });
});

describe('LanguagePicker', () => {
  it('marks the current language and switches on another', () => {
    renderIn('zh-CN', <LanguagePicker />);
    expect(screen.getByRole('radio', { name: '中文' })).toBeChecked();

    fireEvent.click(screen.getByRole('radio', { name: 'English' }));
    expect(cookie()).toBe(`en|${NOW}|p`);
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});
