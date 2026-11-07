'use client';

import { ConfigurationBanner } from '@/components/ui/ConfigurationBanner';
import { Header } from '@/components/ui/Header';
import { SiteFooter } from '@/components/ui/SiteFooter';
import { useModuleRendersRoute } from '@/site-ui/SiteUiBoundary';
import { useConsoleWidth } from '@/components/ui/consoleWidth';

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

  return <ConsoleChrome>{children}</ConsoleChrome>;
}

/**
 * The console's header, constrained main and footer, around a page.
 *
 * Exported for the one other place that draws it: the error page of a route
 * whose chrome the module owns, where the module is what failed.
 */
export function ConsoleChrome({ children }: { children: React.ReactNode }) {
  const consoleWidth = useConsoleWidth();
  return (
    <div className="console-theme flex min-h-screen flex-1 flex-col bg-gray-50 text-gray-900">
      <Header />
      <ConfigurationBanner />
      <main className={`mx-auto flex w-full ${consoleWidth} flex-1 flex-col px-6 py-12`}>
        {children}
      </main>
      <SiteFooter />
    </div>
  );
}
