'use client';

import { usePathname } from 'next/navigation';

import { TermsContent as ConsoleTerms } from '@/app/terms/TermsContent';
import { NeutralAuthCard } from '@/site-ui/neutral/body';
import type { AuthFrameProps, TermsFrameProps } from '@/site-ui/contract';
import { SITE_UI_CLIENT } from '@/site-ui/module';
import { SiteUiProvider } from '@/site-ui/SiteUiCore';
import { publicRouteFor, type PublicRoute } from '@/site-ui/routes';

/**
 * Which route the current pathname is, or `null` when the shared console owns
 * it.
 *
 * The single route lookup in the public tree. `PublicRouteBoundary`, the
 * landing slot and the terms frame all read this, so there is one answer per
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
 * The module's landing page for `/`, or `null` when the console's own landing
 * page should render instead.
 *
 * Kept a separate question from "is this a public route" because replacing the
 * landing page is a decision the module makes by exporting one — the neutral
 * module deliberately does not, and the console's page then stays exactly where
 * it was, with its own tests.
 */
export function useLanding(): (typeof SITE_UI_CLIENT)['Landing'] {
  const route = usePublicRoute();
  return route === 'landing' ? (SITE_UI_CLIENT.Landing ?? null) : null;
}

/**
 * The module's legal frame for `/terms`, or `null` for the console's own.
 *
 * Tested by identity rather than by module id, which is what lets the route
 * boundary ask "does this route already have a header?" without knowing which
 * deployment is running.
 */
export function useTermsFrame(): React.ComponentType<TermsFrameProps> | null {
  const route = usePublicRoute();
  return route === 'terms' ? (SITE_UI_CLIENT.TermsFrame ?? null) : null;
}

/**
 * Installs the compiled-in module and renders the route it owns.
 *
 * Three outcomes, in order:
 *
 * 1. `/` and the module exports a `Landing` → render it, and nothing else. The
 *    landing page is a whole page: it brings its own header, main and footer.
 * 2. a public route → the children, which are the shared pages. Each account
 *    page draws its own frame through `AuthPageFrame` — or, when the module has
 *    no frame, sits directly in the console container; `/terms` draws its own
 *    through `TermsFrame`. The route boundary has already decided who owns the
 *    chrome, so exactly one layer is drawing it.
 * 3. a console route → the children, with the console chrome the route
 *    boundary rendered around them.
 *
 * A module's `PublicFrame` is used by the module's *own* pages and never by the
 * host: wrapping a shared page in it would nest the design's full-width
 * background inside the console container it exists to escape.
 */
export function SiteUiBoundary({ children }: { children: React.ReactNode }) {
  const Landing = useLanding();

  return <SiteUiProvider>{Landing ? <Landing /> : children}</SiteUiProvider>;
}

/**
 * The legal page, in whichever shape the compiled-in module wants.
 *
 * Two cases, and the module's frame receives **no children**:
 *
 * - a module supplies a `TermsFrame` → it renders the whole legal page,
 *   including the text. A layout and its copy travel together, so the host has
 *   nothing to hand over; the frame is the page.
 * - no frame → the console's own body, which is its card inside the console
 *   container, because `PublicRouteBoundary` has already decided that this
 *   route keeps the console chrome.
 *
 * The earlier version passed the host's own legal body into the module's frame.
 * That is two layouts in one page: the console's card inside a full-width
 * legal design, in the wrong column, with a duplicated heading and a second set
 * of `#terms-s*` anchors for the browser to choose between.
 *
 * A client component rather than a server one because the choice depends on what
 * the build compiled in, which is a module-scope fact on the client side.
 */
export function TermsPageContent() {
  const Frame = useTermsFrame();
  if (!Frame) return <ConsoleTerms />;
  return <Frame>{null}</Frame>;
}

/**
 * The module's account frame, or `null` when the console container is the page.
 *
 * The same question `PublicRouteBoundary` asks before it decides to step aside,
 * asked through the same single route lookup, so the two cannot disagree about
 * who is drawing the header. `null` is an answer, not a failure to load: see the
 * contract's `AuthFrame`.
 *
 * Route-aware because the boundary is — it decides once, for a pathname, and
 * this reads the same decision back. `AuthPageFrame` deliberately does *not*
 * call this: an account page already knows it is one, and routing its frame
 * through `usePathname` would make every page that renders a form depend on a
 * navigation mock to be testable.
 */
export function useAuthFrame(): React.ComponentType<AuthFrameProps> | null {
  const route = usePublicRoute();
  if (route === null || !ACCOUNT_ROUTES.has(route)) return null;
  return SITE_UI_CLIENT.AuthFrame ?? null;
}

/** The routes `AuthPageFrame` frames, as route keys rather than paths. */
const ACCOUNT_ROUTES = new Set<PublicRoute>([
  'login',
  'signup',
  'forgot-password',
  'reset-password',
  'verify-email',
]);

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
  // Straight from the module rather than through `useAuthFrame`: `undefined` and
  // `null` both mean "no frame", and a page that renders a form should not need
  // the router to answer that.
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
