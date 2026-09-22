'use client';

import { usePathname } from 'next/navigation';

import { Header } from '@/components/ui/Header';
import { SiteFooter } from '@/components/ui/SiteFooter';
import { useAuthFrame, useLanding, useTermsFrame } from '@/site-ui/SiteUiBoundary';
import { publicRouteFor, type PublicRoute } from '@/site-ui/routes';

/**
 * The route boundary.
 *
 * This replaces the previous `LayoutChrome`, and the difference is the point of
 * the whole refactor: that component decided which chrome to draw by reading
 * `branding.presentation.preset` out of the runtime document and asking whether
 * it equalled `'inference'`. A deployment's *name* was the branch condition, so
 * the shared repository had to know which designs existed.
 *
 * Now the decision is structural. Either the build compiled in a UI module that
 * owns these routes, or it did not. No module identifier is compared to
 * anything.
 *
 * ## Exactly one layer owns header / main / footer
 *
 * Getting this wrong produces two headers or none, and it is the first thing a
 * reviewer should check:
 *
 * - `/` — whoever drew the page: a module's `Landing`, or the console's own;
 * - a module's account routes — its `AuthFrame` is the whole page;
 * - `/terms` with a module that supplies `TermsFrame` — that frame brings its
 *   own legal header and layout;
 * - everything else — the console chrome below.
 *
 * The three page-owning routes are therefore one question asked three times:
 * did the module draw this page? Nothing here compares a module's name or id.
 */
export function PublicRouteBoundary({ children }: { children: React.ReactNode }) {
  const route = usePublicRoute();
  const landing = useLanding();
  const authFrame = useAuthFrame();
  const termsFrame = useTermsFrame();

  const ownsChrome = ownsItsChrome(route, {
    hasModuleLanding: landing !== null,
    hasModuleAuthFrame: authFrame !== null,
    hasModuleTermsFrame: termsFrame !== null,
  });
  if (ownsChrome) {
    return <>{children}</>;
  }

  return (
    <>
      <Header />
      <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-6 py-12">{children}</main>
      <SiteFooter />
    </>
  );
}

/**
 * Whether the frame for this route already draws the page chrome.
 *
 * Every answer is a statement about a *route key* and about what the module
 * provides — never about its id, and never about a path.
 *
 * This function previously compared the route against the literal strings
 * `'/'` and `'/terms'` while `publicRouteFor` returns `'landing'` and
 * `'terms'`. Neither branch could ever match, so every public route took the
 * `route !== null` path and the shared header, main and footer were dropped:
 * the console's own landing page and terms page rendered with no chrome at all,
 * and only a module that happened to frame itself looked right. The type of the
 * parameter is what should have caught it, which is why it now takes
 * `PublicRoute` rather than a string.
 *
 * The account routes were the second half of the same mistake, fixed later: the
 * case returned `true` unconditionally, on the reasoning that the shared page
 * renders `AuthFrame` and nothing else. That is true, and it does not follow —
 * the neutral module's frame is a card, so "the frame is the whole page" left
 * five pages with no header, no `<main>` and no footer. The question is not
 * *whether a frame rendered* but *whether that frame drew the page*, and only
 * the module can answer it. Hence `hasModuleAuthFrame`.
 */
function ownsItsChrome(
  route: PublicRoute | null,
  provided: {
    hasModuleLanding: boolean;
    hasModuleAuthFrame: boolean;
    hasModuleTermsFrame: boolean;
  },
): boolean {
  switch (route) {
    // `/` is owned by whoever drew the page. A module's landing page is a whole
    // page; the console's is a body inside the console container. So this is
    // the one route where the answer depends on *which* page rendered.
    case 'landing':
      return provided.hasModuleLanding;
    // Same question, same reason: a module's legal frame brings its own header,
    // contents list and footer, while the console's body expects the container.
    case 'terms':
      return provided.hasModuleTermsFrame;
    // And again for the account routes: a module whose `AuthFrame` draws a whole
    // page owns these routes, and one whose frame is a card inside the console
    // container does not. The neutral module is the second kind.
    case 'login':
    case 'signup':
    case 'forgot-password':
    case 'reset-password':
    case 'verify-email':
      return provided.hasModuleAuthFrame;
    // A console route. The shared chrome is the only chrome it gets.
    default:
      return false;
  }
}

/**
 * The public route for the current pathname, or `null` for a console route.
 *
 * Exported from here as well as from `SiteUiBoundary`, because this is the
 * module a reader looking for "which chrome does this path get" opens first.
 */
export function usePublicRoute() {
  const pathname = usePathname();
  return publicRouteFor(pathname ?? '/');
}
