'use client';

import { useMemo } from 'react';

import { documentLocale, useLocaleState } from '@/components/providers/LocaleProvider';
import { localeForTag, SOURCE_LOCALE } from '@/lib/i18n/locales';
import { catalogTranslator, type Translate } from '@/lib/i18n/translate';
import { useIsConsoleCopy, useModuleRendersRouteContext } from '@/site-ui/appearance';
import { SITE_UI_CLIENT } from '@/site-ui/module';

/** Whether two BCP-47 tags name the same supported language, or are the same tag. */
function sameLanguage(a: string, b: string): boolean {
  if (a === b) return true;
  const la = localeForTag(a);
  return la !== null && la === localeForTag(b);
}

/**
 * Resolve one interface string, in a component.
 *
 * Three sources, in order: the compiled-in UI module's own wording, the
 * console's catalog for the page's language, then the English literal the call
 * site passes. A key nobody translated resolves to that literal, which is what
 * keeps a partially translated language usable.
 *
 * Each source applies only to a page in its own language (`documentLocale`):
 *
 * - The module's `authMessages` are written in the module's language — its
 *   descriptor `locale`, or English when it has none — and only reach the
 *   public routes. Console routes (marked by `SiteUiBoundary` when
 *   `usePublicRoute()` is `null`) never take them, even when they mount a
 *   shared component such as `PhoneAuthForm`, and a reader who chose another
 *   language than the module's does not get the module's sentences mixed in.
 * - The catalog is the reader's language. A route the module renders in its
 *   own fixed language does not take it.
 *
 * Copy ships with the image that renders it — the module's in the module, the
 * console's in `src/lib/i18n/messages/` — never through `/site-config`, so the
 * version lock can tell a running image is stale (see
 * `docs/developer/distribution-customization.md`).
 *
 * `SITE_UI_CLIENT.authMessages` holds only the keys the contract declares —
 * `module.tsx` drops the rest — so a module can supply wording only for those.
 */
export function useT(): Translate {
  const isConsoleRoute = useIsConsoleCopy();
  const moduleRenders = useModuleRendersRouteContext();
  const { locale, catalog, moduleLocale } = useLocaleState();
  const pageLocale = documentLocale(moduleRenders, moduleLocale, locale);

  const moduleCopyLocale = SITE_UI_CLIENT.descriptor.locale || SOURCE_LOCALE;
  const overrides =
    !isConsoleRoute && sameLanguage(pageLocale, moduleCopyLocale)
      ? SITE_UI_CLIENT.authMessages
      : undefined;
  const pageCatalog = pageLocale === locale ? catalog : undefined;

  return useMemo(() => catalogTranslator(pageCatalog, overrides), [pageCatalog, overrides]);
}

export type { Translate };
