// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

/**
 * Which layer draws the chrome, on every public route, in each shape a module
 * can have.
 *
 * This file exists because the answer was wrong for every public route at the
 * same time and nothing caught it. `ownsItsChrome` compared the route against
 * the literals `'/'` and `'/terms'` while `publicRouteFor` returns `'landing'`
 * and `'terms'`, so neither branch could match and the console's own landing and
 * terms pages rendered with no header, main or footer. Its own screenshot
 * comparison did not catch it either, because *both* sides of that comparison
 * were the distribution's module — the regression was in the neutral build,
 * which no capture covered.
 *
 * ## What this file got wrong the first time
 *
 * The first version of these assertions mocked the module with a frame of its
 * own making and then asserted that the five account routes should have **no**
 * header or footer. That is the regression written down as an expectation: the
 * neutral module's real frame is a card, and a card is not a page. The test
 * passed while five default pages shipped without chrome, which is worse than no
 * test — it is a test that would have caught the fix and failed it.
 *
 * So the module below is not a stand-in with a convenient shape. It is built by
 * the same two rules the real ones are: the neutral module is described exactly
 * as the host will see it (`authFrame: null`), and a distribution is described
 * as one that supplies a whole-page frame. Both are rendered through the real
 * boundary, and the property asserted is a *count* on every route — one header,
 * one `<main>`, one footer — because "a header exists" passes with two.
 */

const pathname = vi.hoisted(() => ({ value: '/' }));

/**
 * The compiled-in module, as `SiteUiBoundary` reads it.
 *
 * Kept mutable rather than fixed so one file can assert both module shapes. The
 * neutral entry is the one that matters: `authFrame: null` is the neutral
 * module's actual declaration, and the whole regression is what happens when
 * that is read as "the page frames itself".
 */
const module_ = vi.hoisted(() => ({
  value: {
    landing: null as null | (() => React.ReactNode),
    authFrame: null as null | ((props: { children: React.ReactNode }) => React.ReactNode),
    termsFrame: null as null | ((props: { children: React.ReactNode }) => React.ReactNode),
  },
}));

vi.mock('next/navigation', () => ({
  usePathname: () => pathname.value,
}));

vi.mock('@/components/providers/useT', () => ({
  useT: () => (key: string, fallback: string) => `${key}=${fallback}`,
  translate: (_key: string, fallback: string) => fallback,
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({
    branding: { appName: 'Test Console', logoUrl: '', orgName: '' },
    distribution: { id: 'test', release: '' },
    features: { publicSignup: true, rag: false, agents: false },
  }),
  useBranding: () => ({ appName: 'Test Console', logoUrl: '', orgName: '' }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { loading: false, isAuthenticated: false, user: null } }),
}));

vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { loading: false, isAuthenticated: false, user: null } }),
  useSession: () => ({ loading: false, isAuthenticated: false, user: null }),
}));

vi.mock('@/site-ui/module', () => ({
  SITE_UI_CLIENT: {
    descriptor: { siteUiApi: 1, id: 'test', locale: '' },
    get Landing() {
      return module_.value.landing;
    },
    get AuthFrame() {
      return module_.value.authFrame;
    },
    get TermsFrame() {
      return module_.value.termsFrame;
    },
    PublicFrame: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  },
}));

vi.mock('@/components/ui/Header', () => ({
  Header: () => <header data-testid="console-header" />,
}));
vi.mock('@/components/ui/SiteFooter', () => ({
  SiteFooter: () => <footer data-testid="console-footer" />,
}));

import { SITE_UI_CLIENT } from '@/site-ui/module';

import { PublicRouteBoundary } from './PublicRouteBoundary';
import { AuthPageFrame } from './SiteUiBoundary';

/** Every public route the boundary decides for, plus a console route. */
const ACCOUNT_ROUTES = [
  '/login',
  '/signup',
  '/forgot-password',
  '/reset-password',
  '/verify-email',
];
const PAGE_ROUTES = ['/', ...ACCOUNT_ROUTES, '/terms'];

function renderAt(route: string, child: React.ReactNode = <p>BODY</p>) {
  pathname.value = route;
  return render(<PublicRouteBoundary>{child}</PublicRouteBoundary>);
}

/** The neutral module, as it really declares itself. */
function useNeutralModule() {
  module_.value.landing = null;
  module_.value.authFrame = null;
  module_.value.termsFrame = null;
}

/** A distribution: it draws its whole landing page, account page and legal page. */
function useFullPageModule() {
  module_.value.landing = () => (
    <div data-testid="module-page">
      <header data-testid="module-header" />
      <main data-testid="module-main" />
      <footer data-testid="module-footer" />
    </div>
  );
  module_.value.authFrame = ({ children }) => (
    <div data-testid="module-page">
      <header data-testid="module-header" />
      <main data-testid="module-main">{children}</main>
      <footer data-testid="module-footer" />
    </div>
  );
  module_.value.termsFrame = () => (
    <div data-testid="module-page">
      <header data-testid="module-header" />
      <main data-testid="module-main" />
      <footer data-testid="module-footer" />
    </div>
  );
}

/**
 * Count the page landmarks, whichever layer drew them.
 *
 * Counted together rather than per layer: "the console drew one header" is not
 * the property. The property is that the *page* has exactly one, and the only
 * way to see two is to count them all.
 */
function landmarks() {
  return {
    headers: screen.queryAllByRole('banner').length,
    mains: screen.queryAllByRole('main').length,
    footers: screen.queryAllByRole('contentinfo').length,
  };
}

describe('chrome ownership on the neutral build', () => {
  afterEach(cleanup);

  it.each(PAGE_ROUTES)('%s has exactly one header, main and footer with no module', (route) => {
    useNeutralModule();
    renderAt(route, route === '/' || route === '/terms' ? <ConsoleBody /> : <AccountBody />);

    // The regression, for all seven routes at once. The five account routes were
    // the ones still broken after the landing and terms pages were fixed: the
    // neutral module declares no account frame, so nothing but the console can
    // draw their chrome.
    expect(landmarks()).toEqual({ headers: 1, mains: 1, footers: 1 });
    expect(screen.getByTestId('console-header')).toBeInTheDocument();
    expect(screen.getByTestId('console-footer')).toBeInTheDocument();
  });

  it('keeps the console chrome while the neutral module draws no page, on the two routes that were fixed first', () => {
    useNeutralModule();
    for (const route of ['/', '/terms']) {
      cleanup();
      renderAt(route);
      expect(landmarks(), route).toEqual({ headers: 1, mains: 1, footers: 1 });
    }
  });

  it('leaves a console route entirely alone', () => {
    useNeutralModule();
    renderAt('/dashboard');

    expect(landmarks()).toEqual({ headers: 1, mains: 1, footers: 1 });
  });

  it('draws nothing around an unknown path, so the 404 is the 404', () => {
    useNeutralModule();
    renderAt('/definitely-not-a-page');

    // A prefix match here would swallow every 404 into the home page.
    expect(screen.getByTestId('console-header')).toBeInTheDocument();
    expect(screen.getByText('BODY')).toBeInTheDocument();
  });
});

describe('chrome ownership when a module draws whole pages', () => {
  afterEach(cleanup);

  it.each(PAGE_ROUTES)(
    '%s has exactly one header, main and footer, drawn by the module',
    (route) => {
      useFullPageModule();
      // `/` and `/terms` are the routes where the *module* draws the page and the
      // shared tree contributes nothing, so the child is the module's own page —
      // which is what `SiteUiBoundary` renders instead of `children`.
      const child =
        route === '/' ? <ModuleLanding /> : route === '/terms' ? <ModuleTerms /> : <AccountBody />;
      renderAt(route, child);

      // The other half of the same rule, and the reason the decision is "did the
      // module draw this page" rather than "is this a module build": a module that
      // frames these routes must not also receive the console container, or the
      // page has two headers. Counting all landmarks is what makes both halves one
      // assertion instead of two that can drift.
      expect(landmarks()).toEqual({ headers: 1, mains: 1, footers: 1 });
      expect(screen.getByTestId('module-header')).toBeInTheDocument();
      expect(screen.queryByTestId('console-header')).toBeNull();
      expect(screen.queryByTestId('console-footer')).toBeNull();
    },
  );

  it('does not let the console chrome back in on a console route', () => {
    useFullPageModule();
    renderAt('/dashboard');

    expect(landmarks()).toEqual({ headers: 1, mains: 1, footers: 1 });
    expect(screen.getByTestId('console-header')).toBeInTheDocument();
  });
});

describe('the account pages inside both module shapes', () => {
  afterEach(cleanup);

  it('renders the shared form inside the console container when the module has no frame', () => {
    useNeutralModule();
    renderAt('/login', <AccountBody />);

    const main = screen.getByRole('main');
    // The form is in the console's `<main>` and there is no module frame around
    // it — which is what "the container is the page" means concretely.
    expect(within(main).getByText('FORM')).toBeInTheDocument();
    expect(screen.queryByTestId('module-page')).toBeNull();
  });

  it('renders the shared form inside the module frame when the module supplies one', () => {
    useFullPageModule();
    renderAt('/login', <AccountBody />);

    const page = screen.getByTestId('module-page');
    expect(within(page).getByText('FORM')).toBeInTheDocument();
    // And the console's own `<main>` is gone, so the form is not nested in two.
    expect(screen.queryByTestId('console-header')).toBeNull();
  });
});

/**
 * The console's own landing or legal body: a *body*, not a page.
 *
 * It expects the container, which is the whole reason the boundary has to keep
 * drawing chrome for these routes when the module provides no page of its own.
 */
function ConsoleBody() {
  return <p>CONSOLE BODY</p>;
}

/**
 * The module's own landing page, as `SiteUiBoundary` renders it.
 *
 * The host renders `Landing` in place of `children`, so a test of chrome
 * ownership on `/` has to put that component in the tree or it is measuring an
 * empty page.
 */
function ModuleLanding() {
  const Landing = SITE_UI_CLIENT.Landing;
  return Landing ? <Landing /> : <p>NO LANDING</p>;
}

/** The module's legal page: the frame receives no children. */
function ModuleTerms() {
  const Frame = SITE_UI_CLIENT.TermsFrame;
  return Frame ? <Frame>{null}</Frame> : <p>NO TERMS FRAME</p>;
}

/** What a shared account page renders into the frame: the page's own form. */
function AccountBody() {
  return (
    <AuthPageFrame
      page="login"
      kicker="KICKER"
      title="TITLE"
      subtitle="SUBTITLE"
      topbar={<span>TOPBAR</span>}
      legal={<span>LEGAL</span>}
    >
      <form>
        <p>FORM</p>
      </form>
    </AuthPageFrame>
  );
}
