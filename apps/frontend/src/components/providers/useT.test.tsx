// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Locale } from '@/lib/i18n/locales';
import type { Catalog } from '@/lib/i18n/translate';

/**
 * Which wording a page gets: the module's, the catalog's or the English at the
 * call site — decided by the language the page is in.
 */

const module_ = vi.hoisted(() => ({ locale: '' }));

vi.mock('@/site-ui/module', () => ({
  SITE_UI_CLIENT: {
    get descriptor() {
      return { siteUiApi: 1, id: 'stand-in', locale: module_.locale };
    },
    authMessages: { 'auth.login.title': 'Module sign-in' },
  },
}));

import { LocaleProvider } from './LocaleProvider';
import { useT } from './useT';
import { ConsoleCopyProvider, ModuleRendersRouteProvider } from '@/site-ui/appearance';

const catalog: Catalog = { 'auth.login.title': { source: 'Log In', text: '登录' } };

function Title() {
  const t = useT();
  return <h1>{t('auth.login.title', 'Log In')}</h1>;
}

function renderTitle({
  locale,
  moduleLocale = '',
  consoleRoute = false,
  moduleRenders = false,
}: {
  locale: Locale;
  moduleLocale?: string;
  consoleRoute?: boolean;
  moduleRenders?: boolean;
}) {
  module_.locale = moduleLocale;
  const { unmount } = render(
    <LocaleProvider
      locale={locale}
      catalog={locale === 'zh-CN' ? catalog : undefined}
      moduleLocale={moduleLocale}
    >
      <ConsoleCopyProvider value={consoleRoute}>
        <ModuleRendersRouteProvider value={moduleRenders}>
          <Title />
        </ModuleRendersRouteProvider>
      </ConsoleCopyProvider>
    </LocaleProvider>,
  );
  const text = screen.getByRole('heading').textContent;
  unmount();
  return text;
}

afterEach(cleanup);

describe('useT', () => {
  it("gives a Chinese reader the catalog's wording", () => {
    expect(renderTitle({ locale: 'zh-CN' })).toBe('登录');
    expect(renderTitle({ locale: 'zh-CN', consoleRoute: true })).toBe('登录');
  });

  it("gives an English reader the module's English rewording on a public route", () => {
    expect(renderTitle({ locale: 'en' })).toBe('Module sign-in');
  });

  it("keeps the module's English away from a reader who chose Chinese", () => {
    expect(renderTitle({ locale: 'zh-CN', moduleRenders: true })).toBe('登录');
  });

  it("never gives a console route the module's wording", () => {
    expect(renderTitle({ locale: 'en', consoleRoute: true })).toBe('Log In');
  });

  it("writes a route the module renders in its fixed language in the module's words", () => {
    expect(renderTitle({ locale: 'zh-CN', moduleLocale: 'fr', moduleRenders: true })).toBe(
      'Module sign-in',
    );
  });

  it("does not mix a fixed-language module's words into a page in another language", () => {
    expect(renderTitle({ locale: 'en', moduleLocale: 'fr' })).toBe('Log In');
    expect(renderTitle({ locale: 'zh-CN', moduleLocale: 'fr' })).toBe('登录');
  });
});
