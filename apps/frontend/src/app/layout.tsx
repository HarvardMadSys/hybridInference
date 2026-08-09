import '../styles/globals.css';
import Script from 'next/script';
import { Crimson_Text } from 'next/font/google';
import { branding } from '@/config/branding';
import { config } from '@/config/env';
import { Providers } from '@/components/providers';
import { ErrorBoundary } from '@/components/ui/ErrorBoundary';
import { Header } from '@/components/ui/Header';
import { SiteFooter } from '@/components/ui/SiteFooter';

const crimsonText = Crimson_Text({
  subsets: ['latin'],
  weight: ['400', '600', '700'],
  variable: '--font-serif',
  display: 'swap',
});

export const metadata = {
  title: config.appName,
  description: branding.appDescription,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`h-full ${crimsonText.variable}`}>
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
          <Providers>
            <Header />
            <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-6 py-12">
              {children}
            </main>
            <SiteFooter />
            {branding.statcounterProjectId && (
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
            )}
          </Providers>
        </ErrorBoundary>
      </body>
    </html>
  );
}
