import { cookies, headers } from 'next/headers';

import { LOCALE_COOKIE, resolveRequestLocale } from './choice';
import type { Locale } from './locales';
import type { Catalog } from './translate';
import zhCN from './messages/zh-CN.json';

/**
 * Each language's catalog, compiled into the image like the rest of the copy.
 * Imported only here, on the server: a client receives the one catalog its
 * request is in, as a prop, rather than every language in its bundle.
 */
const CATALOGS: Readonly<Partial<Record<Locale, Catalog>>> = {
  'zh-CN': zhCN as Catalog,
};

/** A language's catalog; `undefined` for English, which is the source. */
export function catalogFor(locale: Locale): Catalog | undefined {
  return CATALOGS[locale];
}

/** This request's console language: the reader's choice, else their browser's. */
export async function requestLocale(): Promise<Locale> {
  const [cookieStore, headerList] = await Promise.all([cookies(), headers()]);
  return resolveRequestLocale({
    cookie: cookieStore.get(LOCALE_COOKIE)?.value,
    acceptLanguage: headerList.get('accept-language'),
  });
}
