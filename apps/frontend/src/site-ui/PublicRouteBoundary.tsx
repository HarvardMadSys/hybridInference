'use client';

import { usePathname } from 'next/navigation';

import { Header } from '@/components/ui/Header';
import { SiteFooter } from '@/components/ui/SiteFooter';
import { useAuthFrame, useLanding, useTermsFrame } from '@/site-ui/SiteUiBoundary';
import { publicRouteFor, type PublicRoute } from '@/site-ui/routes';

/**
 * Exactly one layer owns the header, main and footer. A supplied Landing,
 * AuthFrame or TermsFrame owns its public route's chrome; all other routes use
 * the shared console chrome. Module identifiers do not affect this decision.
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

/** Whether a provided component owns the complete chrome for this route. */
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
