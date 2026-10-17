import { cookies, headers } from 'next/headers';

import { LOCALE_COOKIE, resolveRequestLocale } from './choice';
import type { Locale } from './locales';
import { catalogTranslator, type Catalog, type Translate } from './translate';
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

/**
 * A translator for a server component, in this request's language — what
 * `useT` is to a client one. It has no module wording: a module's messages
 * reach only the public pages, which are client components.
 */
export async function requestTranslator(): Promise<Translate> {
  return catalogTranslator(catalogFor(await requestLocale()));
}

/** This request's console language: the reader's choice, else their browser's. */
export async function requestLocale(): Promise<Locale> {
  const [cookieStore, headerList] = await Promise.all([cookies(), headers()]);
  return resolveRequestLocale({
    cookie: cookieStore.get(LOCALE_COOKIE)?.value,
    acceptLanguage: headerList.get('accept-language'),
  });
}
