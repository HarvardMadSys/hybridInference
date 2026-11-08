'use client';

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

const COUNTER_SRC = 'https://www.statcounter.com/counter/counter.js';

// Once per document: the counter counts the page view it loads on.
let counterStarted = false;

/**
 * Run `start` once the page has loaded and the browser is idle, as
 * `next/script`'s `lazyOnload` does -- but cancellable, which that is not: a
 * lazy script it has scheduled is added whatever page the visitor is on by
 * then. Returns the cancel.
 */
function afterLoadWhenIdle(start: () => void): () => void {
  let cancelIdle: (() => void) | null = null;
  const schedule = () => {
    if (typeof window.requestIdleCallback === 'function') {
      const handle = window.requestIdleCallback(start);
      cancelIdle = () => window.cancelIdleCallback(handle);
    } else {
      const handle = window.setTimeout(start, 1);
      cancelIdle = () => window.clearTimeout(handle);
    }
  };
  if (document.readyState === 'complete') schedule();
  else window.addEventListener('load', schedule, { once: true });
  return () => {
    window.removeEventListener('load', schedule);
    cancelIdle?.();
  };
}

/**
 * Add StatCounter's script, with the settings it reads from the page. They are
 * set as values rather than written into an inline script, so nothing in them
 * can run.
 */
function startCounter(
  project: number,
  securityKey: string,
  nonce: string | undefined,
  onLoad: () => void,
): void {
  if (counterStarted) return;
  counterStarted = true;
  Object.assign(window, { sc_project: project, sc_invisible: 1, sc_security: securityKey });
  const script = document.createElement('script');
  script.id = 'statcounter-loader';
  script.src = COUNTER_SRC;
  script.async = true;
  if (nonce) script.nonce = nonce;
  script.addEventListener('load', onLoad, { once: true });
  document.body.appendChild(script);
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
 *
 * It loads once the page has loaded and the browser is idle, and only if the
 * page is still a public one at that moment: a visitor quick to click "Sign in"
 * is on the sign-in form by then.
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

  useEffect(() => {
    // A project id is a number; StatCounter counts nothing with anything else.
    const project = Number(projectId);
    if (!eligible || !Number.isSafeInteger(project) || project <= 0) return;
    return afterLoadWhenIdle(() => {
      // The page now, not the page this was scheduled on.
      if (!isAnalyticsPath(window.location.pathname)) return;
      startCounter(project, securityKey, nonce, () => {
        loaded.current = true;
      });
    });
  }, [eligible, projectId, securityKey, nonce]);

  if (!eligible || !isAnalyticsPath(pathname)) return null;

  return (
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
  );
}
