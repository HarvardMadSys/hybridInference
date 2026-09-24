// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Component, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * A module component that throws is one failed page, not a failed site.
 *
 * Each page renders the module's part of it, so the failure is thrown inside
 * the page, where the route's error boundary — `app/error.tsx` — catches it.
 * These render the tree the way the root layout does, with a stand-in for
 * Next's segment boundary around the page, against a module whose components
 * throw.
 */

const pathname = vi.hoisted(() => ({ value: '/' }));

vi.mock('next/navigation', () => ({
  usePathname: () => pathname.value,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock('next/script', () => ({ default: () => null }));
vi.mock('@/lib/api/auth', () => ({ signup: vi.fn() }));
vi.mock('@/components/ui/Header', () => ({
  Header: () => <header data-testid="console-header" />,
}));
vi.mock('@/components/ui/SiteFooter', () => ({
  SiteFooter: () => <footer data-testid="console-footer" />,
}));

const descriptor = { siteUiApi: 1, id: 'failing', locale: 'fr' };

function Fails(): never {
  throw new Error('the module failed to render');
}

function Frame({ children }: { children?: ReactNode }) {
  return (
    <div data-testid="module-page">
      <header />
      <main>{children}</main>
      <footer />
    </div>
  );
}

/** Compile in a module with these exports; one import at a time, as `module.test.tsx` does. */
async function compileIn(exports: Record<string, unknown>) {
  vi.resetModules();
  vi.doMock('@site-ui/client', () => ({
    Landing: undefined,
    AuthFrame: undefined,
    TermsFrame: undefined,
    TermsContent: undefined,
    consentItems: undefined,
    fieldLayout: undefined,
    authAppearance: undefined,
    authMessages: undefined,
    descriptor,
    ...exports,
  }));
  const { SiteUiBoundary } = await import('./SiteUiBoundary');
  const { PublicRouteBoundary } = await import('./PublicRouteBoundary');
  const { default: RouteError } = await import('@/app/error');

  /** Next's segment boundary, reduced to what it does with `error.tsx`. */
  class SegmentBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
    state = { failed: false };
    static getDerivedStateFromError() {
      return { failed: true };
    }
    render() {
      return this.state.failed ? (
        <RouteError error={new Error('caught')} reset={() => this.setState({ failed: false })} />
      ) : (
        this.props.children
      );
    }
  }

  /** The root layout's tree for a path, with the route's page inside its boundary. */
  return (path: string, page: ReactNode) => {
    pathname.value = path;
    return render(
      <SiteUiBoundary>
        <PublicRouteBoundary>
          <SegmentBoundary>{page}</SegmentBoundary>
        </PublicRouteBoundary>
      </SiteUiBoundary>,
    );
  };
}

function landmarks() {
  return {
    headers: screen.queryAllByRole('banner').length,
    mains: screen.queryAllByRole('main').length,
    footers: screen.queryAllByRole('contentinfo').length,
  };
}

function expectErrorPage() {
  const heading = screen.getByRole('heading', {
    level: 1,
    name: 'This page could not be displayed',
  });
  // The console's message, in the console's language, whatever the document's.
  expect(heading.closest('[lang]')).toHaveAttribute('lang', 'en');
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  // One page, in the console's chrome: the module that owned it is what failed.
  expect(landmarks()).toEqual({ headers: 1, mains: 1, footers: 1 });
  expect(screen.getByTestId('console-header')).toBeInTheDocument();
}

/** Development React re-throws a caught error on `window`; jsdom prints it unless it is cancelled. */
function cancel(event: ErrorEvent) {
  event.preventDefault();
}

beforeEach(() => {
  // React reports every error a boundary catches; these are the point.
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  window.addEventListener('error', cancel);
});

afterEach(() => {
  cleanup();
  window.removeEventListener('error', cancel);
  vi.doUnmock('@site-ui/client');
  vi.restoreAllMocks();
});

describe('a module component that throws', () => {
  it('on the home page is caught by the route, not the root layout', async () => {
    const renderRoute = await compileIn({ Landing: Fails });
    const { default: HomePage } = await import('@/app/page');

    renderRoute('/', <HomePage />);

    expectErrorPage();
  });

  it('in the legal text is an error page, never the console’s terms', async () => {
    const renderRoute = await compileIn({
      TermsFrame: Frame,
      TermsContent: Fails,
      consentItems: [{ id: 'terms', label: 'I accept the terms.' }],
    });
    const { default: TermsPage } = await import('@/app/terms/page');

    renderRoute('/terms', <TermsPage />);

    expectErrorPage();
    expect(screen.queryByText(/logging and data use/i)).toBeNull();
    expect(screen.queryByText(/terms of service/i)).toBeNull();
  });

  it('in the sign-up step’s legal text is an error page, never the console’s confirmations', async () => {
    const renderRoute = await compileIn({
      AuthFrame: Frame,
      TermsFrame: Frame,
      TermsContent: Fails,
      consentItems: [{ id: 'terms', label: 'I accept the terms.' }],
    });
    const { default: SignupPage } = await import('@/app/signup/page');

    renderRoute('/signup', <SignupPage />);

    expectErrorPage();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByText(/at least 18 years old/i)).toBeNull();
    expect(screen.queryByText(/research participation/i)).toBeNull();
  });

  it('in a field layout is an error page for that account page', async () => {
    const renderRoute = await compileIn({ AuthFrame: Frame, fieldLayout: Fails });
    const { default: ForgotPasswordPage } = await import('@/app/forgot-password/page');

    renderRoute('/forgot-password', <ForgotPasswordPage />);

    expectErrorPage();
    expect(screen.queryByTestId('module-page')).toBeNull();
  });

  it('is shown in place of the page only, and Try again renders the page again', async () => {
    let fail = true;
    function Landing() {
      if (fail) throw new Error('the module failed to render');
      return <p>MODULE LANDING</p>;
    }
    const renderRoute = await compileIn({ Landing });
    const { default: HomePage } = await import('@/app/page');

    renderRoute('/', <HomePage />);
    expectErrorPage();

    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(screen.getByText('MODULE LANDING')).toBeInTheDocument();
    expect(screen.queryByTestId('console-header')).toBeNull();
  });
});

describe('the root layout', () => {
  it('renders no module component, so none can fail above the routes', async () => {
    const renderRoute = await compileIn({ Landing: Fails });

    // The root layout's tree around a page that is not the module's.
    renderRoute('/', <p>PAGE</p>);

    expect(screen.getByText('PAGE')).toBeInTheDocument();
  });

  it('keeps a console page’s own failure inside the console chrome', async () => {
    const renderRoute = await compileIn({ Landing: Fails });

    renderRoute('/dashboard', <Fails />);

    expectErrorPage();
  });
});
