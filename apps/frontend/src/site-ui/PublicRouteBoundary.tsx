'use client';

import { ConfigurationBanner } from '@/components/ui/ConfigurationBanner';
import { Header } from '@/components/ui/Header';
import { SiteFooter } from '@/components/ui/SiteFooter';
import { useModuleRendersRoute } from '@/site-ui/SiteUiBoundary';
import { isChatWorkspaceRoute, useConsoleWidth } from '@/components/ui/consoleWidth';
import { usePathname } from 'next/navigation';

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
  const workspace = isChatWorkspaceRoute(usePathname() ?? '');
  return (
    <div
      className={`console-theme flex flex-col bg-gray-50 text-gray-900 ${workspace ? 'h-dvh min-h-0 flex-none overflow-hidden' : 'min-h-screen flex-1'}`}
    >
      <Header compact={workspace} />
      <div className="shrink-0">
        <ConfigurationBanner />
      </div>
      <main
        className={
          workspace
            ? 'flex min-h-0 w-full flex-1 flex-col overflow-hidden'
            : `mx-auto flex w-full ${consoleWidth} flex-1 flex-col px-4 py-6 sm:px-6 sm:py-12`
        }
      >
        {children}
      </main>
      {!workspace && <SiteFooter />}
    </div>
  );
}
