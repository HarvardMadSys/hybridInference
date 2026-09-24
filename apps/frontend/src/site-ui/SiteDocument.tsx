'use client';

import { useModuleRendersRoute } from '@/site-ui/SiteUiBoundary';

/** The console's language: every console page is written in English. */
export const CONSOLE_LANGUAGE = 'en';

/**
 * The document element, in the language of whoever renders the route.
 *
 * A page here has one of two authors. The compiled-in module writes the routes
 * it renders — `/` with a `Landing`, the account pages with an `AuthFrame`,
 * `/terms` with its own legal text — in its server `locale`; the console writes
 * every other route, in English. `<html lang>` follows the author, so a French
 * home page is `fr` and the English dashboard beside it stays `en`.
 *
 * A client component, because only the client half of the module can answer
 * "does it render this route": to the server, the module's client exports are
 * references it cannot look inside. It still renders on the server, with the
 * request's pathname, so the first response carries the right language; and a
 * client-side navigation re-renders it with the new pathname, so the attribute
 * follows the route without an effect or a request header.
 *
 * `moduleLocale` is the module's server `locale`, passed in by the root layout.
 * Empty means the module declares no fixed language, and its routes keep the
 * console's.
 */
export function SiteDocument({
  moduleLocale,
  className,
  children,
}: {
  moduleLocale: string;
  className?: string;
  children: React.ReactNode;
}) {
  const moduleRenders = useModuleRendersRoute();
  const lang = moduleRenders && moduleLocale ? moduleLocale : CONSOLE_LANGUAGE;

  return (
    <html lang={lang} className={className}>
      {children}
    </html>
  );
}
