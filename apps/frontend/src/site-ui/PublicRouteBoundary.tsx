'use client';

import { ConsoleNav } from '@/components/features/dashboard/ConsoleNav';
import { useAuth } from '@/components/providers';
import { ConfigurationBanner } from '@/components/ui/ConfigurationBanner';
import { Header } from '@/components/ui/Header';
import { SiteFooter } from '@/components/ui/SiteFooter';
import { useModuleRendersRoute } from '@/site-ui/SiteUiBoundary';
import { CONSOLE_PAGE_WIDTH, useConsoleFrame, useConsoleWidth } from '@/components/ui/consoleWidth';

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
 * The console's header, main and footer, around a page.
 *
 * A dashboard page of the user console is drawn in a shell: the navigation
 * down the left edge of the window, and beside it the page at its own width
 * with the footer under it. Chat instead fills the room beside the navigation
 * and scrolls its own panes, so it is as tall as the window -- with a floor,
 * so a phone held sideways scrolls the page instead of squeezing the
 * conversation away -- and has no footer. Every other route is a centred page.
 *
 * Exported for the one other place that draws it: the error page of a route
 * whose chrome the module owns, where the module is what failed.
 */
export function ConsoleChrome({ children }: { children: React.ReactNode }) {
  const consoleWidth = useConsoleWidth();
  const { shell, workspace } = useConsoleFrame();
  return (
    <div
      className={`console-theme flex flex-col bg-gray-50 text-gray-900 ${
        workspace ? 'h-dvh min-h-[34rem] flex-none overflow-hidden' : 'min-h-screen flex-1'
      }`}
    >
      <Header />
      <ConfigurationBanner />
      {shell ? (
        <ConsoleShell workspace={workspace}>{children}</ConsoleShell>
      ) : (
        <>
          <main
            className={`mx-auto flex w-full ${consoleWidth} flex-1 flex-col px-4 py-6 sm:px-6 sm:py-12`}
          >
            {children}
          </main>
          <SiteFooter />
        </>
      )}
    </div>
  );
}

/**
 * The navigation is the signed-in user's, so it waits for the session: while
 * that loads, and for a visitor about to be sent to sign in, the page has the
 * room to itself.
 */
function ConsoleShell({ workspace, children }: { workspace: boolean; children: React.ReactNode }) {
  const { state } = useAuth();
  return (
    <div className="flex min-h-0 w-full flex-1 flex-col md:flex-row">
      {state.isAuthenticated && <ConsoleNav />}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {workspace ? (
          <main className="flex min-h-0 flex-1 flex-col">{children}</main>
        ) : (
          <>
            <main
              className={`mx-auto flex w-full ${CONSOLE_PAGE_WIDTH} flex-1 flex-col px-4 py-6 sm:px-8 sm:py-8`}
            >
              {children}
            </main>
            <SiteFooter />
          </>
        )}
      </div>
    </div>
  );
}
