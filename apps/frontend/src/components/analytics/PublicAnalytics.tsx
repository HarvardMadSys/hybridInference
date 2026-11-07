'use client';

import Script from 'next/script';
import { usePathname } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

/**
 * The public pages a visitor may be counted on: ones that hold no credential
 * and carry no token. The sign-in, sign-up and password pages are left out --
 * a form for a password, and a reset or verification token in the URL, which
 * the counter would report -- and so is everything behind a sign-in.
 */
const ANALYTICS_PATHS: ReadonlySet<string> = new Set(['/', '/terms', '/stats', '/team']);

export function isAnalyticsPath(pathname: string | null): boolean {
  if (!pathname) return false;
  return ANALYTICS_PATHS.has(pathname === '/' ? '/' : pathname.replace(/\/+$/, ''));
}

/**
 * The deployment's StatCounter, on the public pages only.
 *
 * A third-party script runs with everything the page can reach: the access
 * token in `sessionStorage`, and `/auth/refresh`, which mints a new one from the
 * session cookie. So it loads only in a document that began as a public page
 * for a visitor with no session (`signedOut`, from the request's cookies), and a
 * document it has loaded in never becomes a signed-in one: the console moves
 * between pages without reloading, so leaving the public pages -- for the
 * sign-in form, say -- reloads the page, and the counter with its access goes.
 */
export function PublicAnalytics({
  projectId,
  securityKey,
  nonce,
  signedOut,
}: {
  projectId: string;
  securityKey: string;
  nonce?: string;
  signedOut: boolean;
}) {
  const pathname = usePathname();
  // Decided once, for the page this document was served as.
  const [eligible] = useState(() => signedOut && isAnalyticsPath(pathname));
  const loaded = useRef(false);

  useEffect(() => {
    if (loaded.current && !isAnalyticsPath(pathname)) window.location.reload();
  }, [pathname]);

  if (!eligible || !isAnalyticsPath(pathname)) return null;

  return (
    <>
      <Script id="statcounter-config" strategy="afterInteractive" nonce={nonce}>
        {`var sc_project=${projectId}; var sc_invisible=1; var sc_security='${securityKey}';`}
      </Script>
      <Script
        id="statcounter-loader"
        src="https://www.statcounter.com/counter/counter.js"
        strategy="lazyOnload"
        nonce={nonce}
        onLoad={() => {
          loaded.current = true;
        }}
      />
      <noscript>
        <div className="statcounter">
          <a title="Web Analytics" href="https://statcounter.com/" target="_blank" rel="noreferrer">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              className="statcounter"
              src={`https://c.statcounter.com/${projectId}/0/${securityKey}/1/`}
              alt="Web Analytics"
              referrerPolicy="no-referrer-when-downgrade"
            />
          </a>
        </div>
      </noscript>
    </>
  );
}
