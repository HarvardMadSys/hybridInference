import '../styles/globals.css';
import Script from 'next/script';
import { Crimson_Text } from 'next/font/google';
import { config } from '@/config/env';
import { Providers } from '@/components/providers';
import { ErrorBoundary } from '@/components/ui/ErrorBoundary';
import { BuildInfo } from '@/components/ui/BuildInfo';

const crimsonText = Crimson_Text({
  subsets: ['latin'],
  weight: ['400', '600', '700'],
  variable: '--font-serif',
  display: 'swap',
});

export const metadata = {
  title: config.appName,
  description: 'Free LLM inference for research, built at Harvard SEAS.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`h-full ${crimsonText.variable}`}>
      <head>
        <Script id="statcounter-config" strategy="afterInteractive">
          {"var sc_project=13224568; var sc_invisible=1; var sc_security='2d8ab84a';"}
        </Script>
        <Script
          id="statcounter-loader"
          src="https://www.statcounter.com/counter/counter.js"
          strategy="afterInteractive"
        />
      </head>
      <body
        className="flex min-h-screen flex-col bg-gray-50 text-black antialiased"
        suppressHydrationWarning
      >
        <ErrorBoundary>
          <Providers>
            <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-6">
              <div className="flex items-baseline gap-2">
                <span className="text-xl font-bold tracking-tight">{config.appName}</span>
                <a
                  href="https://madsys.seas.harvard.edu"
                  className="font-serif text-sm text-gray-500 hover:text-crimson"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Harvard SEAS
                </a>
              </div>
            </header>
            <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-6 py-12">
              {children}
            </main>
            <footer className="mx-auto w-full max-w-5xl px-6 py-6 text-center text-sm text-gray-400">
              <div className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1">
                <span>© {config.appName}</span>
                <span aria-hidden="true">·</span>
                <a
                  href="https://madsys.seas.harvard.edu"
                  className="hover:text-crimson"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Harvard SEAS
                </a>
                <span aria-hidden="true">·</span>
                <a
                  href="https://doc.freeinference.org/"
                  className="hover:text-crimson"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Docs
                </a>
                <span aria-hidden="true">·</span>
                <a
                  href="https://github.com/HarvardMadSys/hybridInference"
                  className="hover:text-crimson"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  GitHub
                </a>
                <span aria-hidden="true">·</span>
                <BuildInfo />
              </div>
            </footer>
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
                    src="https://c.statcounter.com/13224568/0/2d8ab84a/1/"
                    alt="Web Analytics"
                    referrerPolicy="no-referrer-when-downgrade"
                  />
                </a>
              </div>
            </noscript>
          </Providers>
        </ErrorBoundary>
      </body>
    </html>
  );
}
