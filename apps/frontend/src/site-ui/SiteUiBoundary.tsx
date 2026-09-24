'use client';

import { usePathname } from 'next/navigation';

import { TermsContent as ConsoleTerms } from '@/app/terms/TermsContent';
import { NeutralAuthCard } from '@/site-ui/neutral/body';
import { SITE_UI_CLIENT } from '@/site-ui/module';
import { SiteUiProvider } from '@/site-ui/SiteUiCore';
import { isAuthRoute, publicRouteFor, type PublicRoute } from '@/site-ui/routes';

/**
 * Which route the current pathname is, or `null` when the shared console owns
 * it.
 *
 * The single route lookup in the public tree. The chrome, the document
 * language and the error page all read this, so there is one answer per
 * render and no chance of two of them disagreeing about where the visitor is.
 *
 * `null` is not an error: `/dashboard`, `/chat`, `/team`, `/authorize`,
 * `/agents` and an unknown path all mean "the console renders this".
 */
export function usePublicRoute() {
  const pathname = usePathname();
  return publicRouteFor(pathname ?? '/');
}

/**
 * Whether the compiled-in module renders this route, rather than the console.
 *
 * The module renders a public route when it exports the component for it, and
 * nothing else: `/` through `Landing`, the five account pages through
 * `AuthFrame`, `/terms` through its legal text. Every other path, and a public
 * route whose export is absent, is the console's.
 *
 * Two things follow the answer, and both read it here so they cannot disagree:
 * who draws the chrome (`PublicRouteBoundary`) and which language the document
 * is in (`SiteDocument`). Tested by identity rather than by module id, so the
 * answer does not depend on knowing which deployment is running.
 */
export function moduleRendersRoute(route: PublicRoute | null): boolean {
  if (route === null) return false;
  // `/` is owned by whoever drew the page. A module's landing page is a whole
  // page; the console's is a body inside the console container.
  if (route === 'landing') return SITE_UI_CLIENT.Landing !== null;
  // Same question, same reason: a module's legal frame brings its own header,
  // contents list and footer, while the console's body expects the container.
  if (route === 'terms') return SITE_UI_CLIENT.legalText !== null;
  // And again for the account routes: a module whose `AuthFrame` draws a whole
  // page owns these routes, and one without a frame does not. The neutral
  // module is the second kind.
  return isAuthRoute(route) && SITE_UI_CLIENT.AuthFrame !== null;
}

/** `moduleRendersRoute` for the current pathname. */
export function useModuleRendersRoute(): boolean {
  return moduleRendersRoute(usePublicRoute());
}

/**
 * Installs the compiled-in module's styling and field layout around the pages.
 *
 * It renders no module component itself. Each page renders the module's part
 * of it — the home page its `Landing`, an account page its `AuthFrame`,
 * `/terms` its legal text — because a component rendered here, in the root
 * layout, is above every route's error boundary: a module that threw would
 * take the whole application down with it rather than one page.
 */
export function SiteUiBoundary({ children }: { children: React.ReactNode }) {
  return <SiteUiProvider>{children}</SiteUiProvider>;
}

/**
 * The legal page, in whichever shape the compiled-in module wants.
 *
 * Two cases:
 *
 * - the module publishes its own legal text → its `TermsFrame` draws the page
 *   around its `TermsContent`, which the host renders and hands in as the
 *   frame's children. The consent step renders the same `TermsContent`, so the
 *   text published here is the text a visitor accepts at sign-up.
 * - it does not → the console's own body, which is its card inside the console
 *   container, because `PublicRouteBoundary` has already decided that this
 *   route keeps the console chrome.
 *
 * A client component rather than a server one because the choice depends on what
 * the build compiled in, which is a module-scope fact on the client side.
 */
export function TermsPageContent() {
  const legal = SITE_UI_CLIENT.legalText;
  if (!legal) return <ConsoleTerms />;
  const { TermsFrame: Frame, TermsContent: Content } = legal;
  return (
    <Frame>
      <Content headingLevel={2} compact={false} />
    </Frame>
  );
}

/**
 * Frame for one account page: the module's `AuthFrame` around the shared form,
 * or the console's default card inside the console container.
 *
 * The one place the tree crosses from a shared controller to distribution
 * presentation, and deliberately narrow. The page keeps ownership of
 * everything that decides whether a user gets in — the schema, the submit call,
 * the error codes, the `next` target — and hands over only what the page looks
 * like.
 *
 * With no module frame the fallback is `NeutralAuthCard`, which is the neutral
 * module's own body. It is imported rather than inlined so there is one
 * definition of the default look, and it is *not* used as a generic fallback for
 * any module that omits a frame: a distribution with its own design and no
 * `AuthFrame` has said the console container is the page, and the card is what
 * that container renders.
 *
 * The default card places the heading and the cross-link, which is where the
 * console has always put them; `kicker` and `legal` are the *frame's* material
 * and it has nowhere to put them, so a module that wants those supplies a
 * frame.
 */
export function AuthPageFrame({
  page,
  kicker,
  title,
  subtitle,
  topbar,
  legal,
  children,
}: {
  page: 'login' | 'signup' | 'forgot-password' | 'reset-password' | 'verify-email';
  kicker: string;
  title: React.ReactNode;
  subtitle: string;
  topbar?: React.ReactNode;
  legal?: React.ReactNode;
  children: React.ReactNode;
}) {
  // Straight from the module rather than through the route: an account page
  // already knows it is one, and a page that renders a form should not need the
  // router to answer whether it has a frame.
  const Frame = SITE_UI_CLIENT.AuthFrame ?? null;
  if (!Frame) {
    return (
      <NeutralAuthCard title={title} subtitle={subtitle} topbar={topbar}>
        {children}
      </NeutralAuthCard>
    );
  }

  return (
    <Frame
      page={page}
      kicker={kicker}
      title={title}
      subtitle={subtitle}
      topbar={topbar}
      legal={legal}
    >
      {children}
    </Frame>
  );
}
