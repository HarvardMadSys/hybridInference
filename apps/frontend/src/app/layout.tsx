import '../styles/globals.css';
import '../site-ui/active/styles.css';
import { Crimson_Text } from 'next/font/google';
import { cookies, headers } from 'next/headers';
import type { Metadata } from 'next';
import { PublicAnalytics } from '@/components/analytics/PublicAnalytics';
import { Providers } from '@/components/providers';
import { THEME_BOOTSTRAP_SCRIPT } from '@/components/providers/theme';
import { ErrorBoundary } from '@/components/ui/ErrorBoundary';
import { PublicRouteBoundary } from '@/site-ui/PublicRouteBoundary';
import { SiteUiBoundary } from '@/site-ui/SiteUiBoundary';
import { SiteDocument } from '@/site-ui/SiteDocument';
import { locale as publicLocale } from '@site-ui/server';
import { loadRuntimeSiteConfig } from '@/config/site-config.server';
import { rootMetadata } from '@/config/site-metadata';
import { catalogFor, requestLocale } from '@/lib/i18n/request.server';
import { consoleSessionCookieNames } from '@/lib/utils/consoleSessionCookie';

const crimsonText = Crimson_Text({
  subsets: ['latin'],
  weight: ['400', '600', '700'],
  variable: '--font-serif',
  display: 'swap',
});

export const dynamic = 'force-dynamic';

export async function generateMetadata(): Promise<Metadata> {
  return rootMetadata(await loadRuntimeSiteConfig());
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const [siteConfig, locale, requestHeaders, cookieStore] = await Promise.all([
    loadRuntimeSiteConfig(),
    requestLocale(),
    headers(),
    cookies(),
  ]);
  const { branding } = siteConfig;
  // This response's script nonce, from src/middleware.ts: the page's policy
  // runs no script without it.
  const nonce = requestHeaders.get('x-nonce') ?? undefined;
  const signedOut = ![...consoleSessionCookieNames()].some((name) => cookieStore.has(name));

  // The document language follows whoever renders the route: the module's
  // server `locale` on the routes the module renders, the reader's language on
  // every other. Only the client half of the module knows which routes those
  // are, so `SiteDocument` — a client component, rendered here on the server
  // too — decides, and decides again on each client-side navigation.

  return (
    <SiteDocument
      moduleLocale={publicLocale}
      consoleLocale={locale}
      className={`h-full ${crimsonText.variable}`}
    >
      <head>
        <script
          id="console-theme"
          nonce={nonce}
          dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }}
        />
      </head>
      <body
        className="flex min-h-screen flex-col bg-gray-50 text-black antialiased"
        suppressHydrationWarning
      >
        <ErrorBoundary>
          <Providers
            initialSiteConfig={siteConfig}
            locale={locale}
            catalog={catalogFor(locale)}
            moduleLocale={publicLocale}
          >
            {/* The compiled-in Site UI first, so the shared forms inside it can
                read the active appearance and wording; then the route boundary,
                which steps aside for the public routes and draws the console
                chrome — header, constrained main, footer — everywhere else. */}
            <SiteUiBoundary>
              <PublicRouteBoundary>{children}</PublicRouteBoundary>
            </SiteUiBoundary>
            {/* Ternary, not `&&`: an unset project id is '', which `&&` would
                render as an empty text node. */}
            {branding.statcounterProjectId ? (
              <PublicAnalytics
                projectId={branding.statcounterProjectId}
                securityKey={branding.statcounterSecurityKey}
                nonce={nonce}
                signedOut={signedOut}
              />
            ) : null}
          </Providers>
        </ErrorBoundary>
      </body>
    </SiteDocument>
  );
}
