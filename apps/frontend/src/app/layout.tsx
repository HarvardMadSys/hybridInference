import '../styles/globals.css';
import '../site-ui/active/styles.css';
import Script from 'next/script';
import { Crimson_Text } from 'next/font/google';
import type { Metadata } from 'next';
import { Providers } from '@/components/providers';
import { ErrorBoundary } from '@/components/ui/ErrorBoundary';
import { PublicRouteBoundary } from '@/site-ui/PublicRouteBoundary';
import { SiteUiBoundary } from '@/site-ui/SiteUiBoundary';
import { locale as publicLocale } from '@site-ui/server';
import { loadRuntimeSiteConfig } from '@/config/site-config.server';
import { rootMetadata } from '@/config/site-metadata';

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
  const siteConfig = await loadRuntimeSiteConfig();
  const { branding } = siteConfig;

  // The document language comes from the server half of the compiled-in Site
  // UI. This is a server module of plain serializable values on purpose: the
  // client half is a component tree, and reading a property off it here would
  // pull hooks and `usePathname` into a module that has no request. Empty means
  // "this UI declares no fixed language" and the console's own language wins.

  return (
    <html lang={publicLocale || 'en'} className={`h-full ${crimsonText.variable}`}>
      <head>
        {/* Ternary, not `&&`: an unset project id is '', and `{'' && …}` renders
            the empty string as a text node. A text node inside <head> is invalid
            HTML, so the parser hoists it out, server and client trees diverge,
            and the failed hydration tears out <head> — stylesheets included,
            leaving the whole app unstyled. Only bites builds without a
            statcounter id, i.e. local and neutral ones. */}
        {branding.statcounterProjectId ? (
          <>
            <Script id="statcounter-config" strategy="afterInteractive">
              {`var sc_project=${branding.statcounterProjectId}; var sc_invisible=1; var sc_security='${branding.statcounterSecurityKey}';`}
            </Script>
            <Script
              id="statcounter-loader"
              src="https://www.statcounter.com/counter/counter.js"
              strategy="lazyOnload"
            />
          </>
        ) : null}
      </head>
      <body
        className="flex min-h-screen flex-col bg-gray-50 text-black antialiased"
        suppressHydrationWarning
      >
        <ErrorBoundary>
          <Providers initialSiteConfig={siteConfig}>
            {/* The compiled-in Site UI first, so the shared forms inside it can
                read the active appearance and wording; then the route boundary,
                which steps aside for the public routes and draws the console
                chrome — header, constrained main, footer — everywhere else. */}
            <SiteUiBoundary>
              <PublicRouteBoundary>{children}</PublicRouteBoundary>
            </SiteUiBoundary>
            {branding.statcounterProjectId ? (
              <noscript>
                <div className="statcounter">
                  <a
                    title="Web Analytics"
                    href="https://statcounter.com/"
                    target="_blank"
                    rel="noreferrer"
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      className="statcounter"
                      src={`https://c.statcounter.com/${branding.statcounterProjectId}/0/${branding.statcounterSecurityKey}/1/`}
                      alt="Web Analytics"
                      referrerPolicy="no-referrer-when-downgrade"
                    />
                  </a>
                </div>
              </noscript>
            ) : null}
          </Providers>
        </ErrorBoundary>
      </body>
    </html>
  );
}
