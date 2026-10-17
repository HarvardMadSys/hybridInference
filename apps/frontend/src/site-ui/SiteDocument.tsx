'use client';

import { documentLocale } from '@/components/providers/LocaleProvider';
import { SOURCE_LOCALE, type Locale } from '@/lib/i18n/locales';
import { useModuleRendersRoute } from '@/site-ui/SiteUiBoundary';

/**
 * The console's source language: every console string is written in English,
 * and a page nobody translated is in it.
 */
export const CONSOLE_LANGUAGE = SOURCE_LOCALE;

/**
 * The document element, in the language of whoever renders the route.
 *
 * A page here has one of two authors. The compiled-in module writes the routes
 * it renders — `/` with a `Landing`, the account pages with an `AuthFrame`,
 * `/terms` with its own legal text — in its server `locale`; the console writes
 * every other route, in the reader's language. `<html lang>` follows the
 * author, so a French home page is `fr` and the dashboard beside it is in
 * whatever language its reader chose.
 *
 * A client component, because only the client half of the module can answer
 * "does it render this route": to the server, the module's client exports are
 * references it cannot look inside. It still renders on the server, with the
 * request's pathname, so the first response carries the right language; and a
 * client-side navigation re-renders it with the new pathname, so the attribute
 * follows the route without an effect or a request header.
 *
 * `moduleLocale` is the module's server `locale`, passed in by the root layout.
 * Empty means the module declares no fixed language, and its routes are in the
 * reader's. `consoleLocale` is the reader's, resolved by the root layout from
 * their choice or their browser.
 */
export function SiteDocument({
  moduleLocale,
  consoleLocale = CONSOLE_LANGUAGE,
  className,
  children,
}: {
  moduleLocale: string;
  consoleLocale?: Locale;
  className?: string;
  children: React.ReactNode;
}) {
  const lang = documentLocale(useModuleRendersRoute(), moduleLocale, consoleLocale);

  return (
    <html lang={lang} className={className}>
      {children}
    </html>
  );
}
