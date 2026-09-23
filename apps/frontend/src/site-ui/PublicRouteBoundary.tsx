'use client';

import { usePathname } from 'next/navigation';

import { Header } from '@/components/ui/Header';
import { SiteFooter } from '@/components/ui/SiteFooter';
import { useModuleRendersRoute } from '@/site-ui/SiteUiBoundary';
import { publicRouteFor } from '@/site-ui/routes';

/**
 * Exactly one layer owns the header, main and footer. A supplied Landing,
 * AuthFrame or legal text owns its public route's chrome; all other routes use
 * the shared console chrome. Module identifiers do not affect this decision.
 *
 * The decision is `moduleRendersRoute`, the same one the document language
 * follows, so a route whose chrome the module draws is also the route in the
 * module's language.
 */
export function PublicRouteBoundary({ children }: { children: React.ReactNode }) {
  if (useModuleRendersRoute()) {
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
 * The public route for the current pathname, or `null` for a console route.
 *
 * Exported from here as well as from `SiteUiBoundary`, because this is the
 * module a reader looking for "which chrome does this path get" opens first.
 */
export function usePublicRoute() {
  const pathname = usePathname();
  return publicRouteFor(pathname ?? '/');
}
