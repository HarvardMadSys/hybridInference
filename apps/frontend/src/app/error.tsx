'use client';

import { Button } from '@/components/ui/Button';
import { ConsoleChrome } from '@/site-ui/PublicRouteBoundary';
import { CONSOLE_LANGUAGE } from '@/site-ui/SiteDocument';
import { useModuleRendersRoute } from '@/site-ui/SiteUiBoundary';

/**
 * A page that failed to render, whichever page it was.
 *
 * Next's error boundary for every route below the root layout, and the one
 * that catches a distribution's UI module failing: its landing page, its
 * account frame or field layout, its terms frame or legal text. Each renders
 * inside its page, so a failure replaces that page and nothing else — the root
 * layout, the session and every other route keep working, and navigating away
 * leaves the error behind.
 *
 * The message is neutral and explicit, and it is the only thing shown. It is
 * never a stand-in for what failed: a page whose legal text did not render
 * shows this, not the console's terms or confirmations, which are not what the
 * deployment publishes or asks a visitor to accept.
 *
 * A route whose chrome the module owns has none left when the module fails, so
 * the page is drawn in the console's; any other route is already inside it.
 * The message is the console's, so it is marked English: the document may be
 * in the module's language on this route.
 *
 * A server render that throws is not caught here: React renders no error
 * boundary on the server. Next answers that request with a 500 and an empty
 * document — or, where the page renders inside its own `<Suspense>`, with a
 * 200 and that boundary's fallback — renders the page again in the browser,
 * and this boundary catches the failure there.
 */
export default function RouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const moduleOwnsChrome = useModuleRendersRoute();
  const message = (
    <section lang={CONSOLE_LANGUAGE} className="mx-auto w-full max-w-md py-12 text-center">
      <h1 className="text-2xl font-semibold tracking-tight text-gray-900">
        This page could not be displayed
      </h1>
      <p className="mt-3 text-sm leading-6 text-gray-600">
        Something went wrong while showing this page. Please try again. If the problem continues,
        contact the site administrator.
      </p>
      <Button className="mt-6" onClick={reset}>
        Try again
      </Button>
    </section>
  );

  return moduleOwnsChrome ? <ConsoleChrome>{message}</ConsoleChrome> : message;
}
