'use client';

import { createContext, useContext, useMemo } from 'react';

import { SOURCE_LOCALE, type Locale } from '@/lib/i18n/locales';
import type { Catalog } from '@/lib/i18n/translate';

/**
 * The console's language for this request, and its catalog.
 *
 * The root layout resolves both on the server — from the reader's choice
 * cookie, else their browser's languages — and hands them down as props, so the
 * server render and the hydrating client agree on the language without the
 * client looking at the cookie or `navigator.language` itself. A new choice
 * refreshes the route, and the layout hands down the new pair.
 *
 * Only the active language's catalog crosses to the client; English has none,
 * because English is the literal at every call site.
 */
interface LocaleState {
  locale: Locale;
  catalog: Catalog | undefined;
  /**
   * The compiled-in module's server `locale`: the language of the routes the
   * module renders, or '' when it has no fixed one. See `documentLocale`.
   */
  moduleLocale: string;
}

const LocaleContext = createContext<LocaleState>({
  locale: SOURCE_LOCALE,
  catalog: undefined,
  moduleLocale: '',
});

export function LocaleProvider({
  locale,
  catalog,
  moduleLocale,
  children,
}: LocaleState & { children: React.ReactNode }) {
  const value = useMemo(() => ({ locale, catalog, moduleLocale }), [locale, catalog, moduleLocale]);
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

/** The internal state, for `useT` and the choice hooks. */
export function useLocaleState(): LocaleState {
  return useContext(LocaleContext);
}

/** The console's language: what the reader chose, or what was chosen for them. */
export function useLocale(): Locale {
  return useContext(LocaleContext).locale;
}

/**
 * The language a page is written in.
 *
 * A route the compiled-in module renders is in the module's fixed language when
 * it declares one; every other page — the console's, and a module route with no
 * fixed language — is in the reader's. `<html lang>` and `useT` both answer
 * from this, so the attribute always names the language the text is in.
 */
export function documentLocale(
  moduleRendersRoute: boolean,
  moduleLocale: string,
  consoleLocale: Locale,
): string {
  return moduleRendersRoute && moduleLocale ? moduleLocale : consoleLocale;
}
