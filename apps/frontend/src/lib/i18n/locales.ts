/**
 * The languages the console is written in, and how a request picks one.
 *
 * Plain data and pure functions, imported by the server root layout and by
 * client components alike, so nothing here may touch `document` or
 * `next/headers`.
 */

/** Every language the console ships a catalog for; English is the source. */
export const SUPPORTED_LOCALES = ['en', 'zh-CN'] as const;

export type Locale = (typeof SUPPORTED_LOCALES)[number];

/** The language of the strings at each call site, and of every fallback. */
export const SOURCE_LOCALE: Locale = 'en';

/**
 * Each language's name, written in that language.
 *
 * Never translated: a reader who switched to a language they cannot read has
 * to be able to find their own language's name to switch back.
 */
export const LOCALE_NAMES: Readonly<Record<Locale, string>> = {
  en: 'English',
  'zh-CN': '中文',
};

export function isLocale(value: unknown): value is Locale {
  return typeof value === 'string' && (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

/**
 * The supported language a BCP-47 tag asks for, or `null`.
 *
 * There is one Chinese: every `zh` tag — `zh-CN`, `zh-Hans`, `zh-TW`, `zh-HK`
 * — reads the same catalog, by decision (see the 2026-10-17 console language
 * design record). Matching is on the primary subtag otherwise.
 */
export function localeForTag(tag: string): Locale | null {
  const primary = tag.trim().toLowerCase().split('-')[0];
  if (primary === 'zh') return 'zh-CN';
  if (primary === 'en') return 'en';
  return null;
}

/**
 * The supported language an `Accept-Language` header prefers most, or `null`.
 *
 * Entries are taken in quality order, ties in header order; `q=0` means "not
 * this one" and is skipped. `*` names no language and is ignored, so a browser
 * that only sends `*` falls through to the deployment default.
 */
export function matchAcceptLanguage(header: string | null | undefined): Locale | null {
  if (!header) return null;
  const ranked = header
    .split(',')
    .map((part, index) => {
      const [tag, ...params] = part.trim().split(';');
      const qParam = params.map((p) => p.trim()).find((p) => p.startsWith('q='));
      const q = qParam ? Number.parseFloat(qParam.slice(2)) : 1;
      return { tag: tag.trim(), q: Number.isFinite(q) ? q : 0, index };
    })
    .filter((entry) => entry.tag && entry.tag !== '*' && entry.q > 0)
    .sort((a, b) => b.q - a.q || a.index - b.index);

  for (const entry of ranked) {
    const locale = localeForTag(entry.tag);
    if (locale) return locale;
  }
  return null;
}
