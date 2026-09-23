// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest';

import { AuthField, AuthLoading, AuthNotice } from '@/components/auth/AuthForm';
import { AuthAppearanceProvider, NEUTRAL_AUTH_APPEARANCE } from '@/site-ui/appearance';
import { NeutralAuthCard } from '@/site-ui/neutral/client';
import { SITE_UI_CLIENT } from '@/site-ui/module';
import { publicRouteFor, publicRoutePaths } from '@/site-ui/routes';
import {
  PUBLIC_ROUTES,
  type SiteUiClientModule,
  type SiteUiServerModule,
} from '@/site-ui/contract';

vi.mock('@/components/providers/useT', () => ({
  useT: () => (key: string, fallback: string) => `${key}=${fallback}`,
}));

describe('public route matching', () => {
  it('maps exactly the routes the contract allows', () => {
    for (const route of PUBLIC_ROUTES) {
      const path = route === 'landing' ? '/' : `/${route}`;
      expect(publicRouteFor(path), path).toBe(route);
    }
    expect(publicRoutePaths()).toHaveLength(PUBLIC_ROUTES.length);
  });

  it('treats one trailing slash as the same route', () => {
    expect(publicRouteFor('/login/')).toBe('login');
    expect(publicRouteFor('/terms//')).toBe('terms');
    expect(publicRouteFor('/')).toBe('landing');
  });

  it('does not claim console routes, unknown paths or prefix lookalikes', () => {
    // A prefix match here would swallow every 404 into the home page, and would
    // let a module frame `/dashboard`, which it does not own.
    for (const path of [
      '/dashboard',
      '/dashboard/admin',
      '/chat',
      '/team',
      '/authorize',
      '/agents',
      '/agents/api/anything',
      '/login-again',
      '/not-a-page',
      '/terms-archive',
    ]) {
      expect(publicRouteFor(path), path).toBeNull();
    }
  });
});

describe('the compiled-in module', () => {
  it('does not advertise unconsumed layout or metadata capabilities', () => {
    expectTypeOf<SiteUiClientModule>().not.toHaveProperty('PublicFrame');
    expectTypeOf<SiteUiServerModule>().not.toHaveProperty('metadata');
    expect(SITE_UI_CLIENT).not.toHaveProperty('PublicFrame');
  });

  it('is the neutral one in this build, and says so', () => {
    expect(SITE_UI_CLIENT.descriptor.siteUiApi).toBe(1);
    expect(SITE_UI_CLIENT.descriptor.id).toBe('neutral');
  });

  it('declares no landing page, which is what keeps the console page', () => {
    // `Landing === null` is an answer, not an omission: the host renders its own
    // page for `/`, and `PublicRouteBoundary` keeps the console chrome around
    // it. Collapsing `null` into `undefined` would make the boundary treat `/`
    // as module-owned and render a page with no chrome at all.
    expect(SITE_UI_CLIENT.Landing).toBeNull();
  });

  it('declares no account frame, which is what keeps the console chrome', () => {
    // No page-owning frame: the console provides chrome around the default card.
    expect(SITE_UI_CLIENT.AuthFrame).toBeNull();
  });

  it('leaves the class map and the field arrangement to the host', () => {
    // The console's look is the host's default, applied whenever a module
    // supplies neither; the neutral module does not restate it, and so does not
    // use the deprecated class map at all.
    expect(SITE_UI_CLIENT.authAppearance).toBeUndefined();
    expect(SITE_UI_CLIENT.fieldLayout).toBeUndefined();
  });

  it('publishes no legal text, which keeps the console’s terms and confirmations', () => {
    // The legal exports are optional and their absence is a statement: the
    // console's terms page is the one this deployment shows, so the console
    // keeps its own container, text and sign-up confirmations. A module that
    // draws a legal frame must also ship the text that goes in it and what a
    // visitor confirms about it, and the neutral module ships none of the three.
    expect(SITE_UI_CLIENT.legalText).toBeNull();
  });
});

describe('the neutral account card', () => {
  afterEach(cleanup);

  it('preserves the default cross-link styling and spacing without changing the supplied node', () => {
    const topbar = (
      <>
        Already have an account? <a href="/login">Log In</a>
      </>
    );
    const { rerender } = render(
      <NeutralAuthCard title="Sign Up" subtitle="Create an account" topbar={topbar}>
        <form>
          <button>Sign Up</button>
        </form>
      </NeutralAuthCard>,
    );
    const link = screen.getByRole('link', { name: 'Log In' });
    expect(link.parentElement).toHaveClass(
      'mt-5',
      'text-gray-600',
      '[&_a]:font-medium',
      '[&_a]:text-blue-600',
      '[&_a:hover]:text-blue-700',
    );
    expect(
      screen.getByRole('button', { name: 'Sign Up' }).compareDocumentPosition(link) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // A module-owned frame receives the same node without neutral-card styles.
    rerender(<div data-testid="custom-frame">{topbar}</div>);
    expect(screen.getByRole('link', { name: 'Log In' })).not.toHaveAttribute('class');
    expect(screen.getByTestId('custom-frame')).not.toHaveAttribute('class');
  });
});

describe('the shared account primitives', () => {
  afterEach(cleanup);

  const fixtureAppearance = {
    ...NEUTRAL_AUTH_APPEARANCE,
    field: 'fixture-field',
    label: 'fixture-label',
    input: 'fixture-input',
    error: 'fixture-error',
    hint: 'fixture-hint',
    noticeError: 'fixture-notice-error',
    loading: 'fixture-loading',
    loadingWrap: 'fixture-loading-wrap',
  };

  function withAppearance(children: React.ReactNode) {
    return render(
      <AuthAppearanceProvider value={fixtureAppearance}>{children}</AuthAppearanceProvider>,
    );
  }

  it('applies the active module’s classes and keeps the ARIA contract', () => {
    withAppearance(
      <AuthField id="email" label="Email" hint="We never share it." error="Required">
        <input id="email" />
      </AuthField>,
    );

    const label = screen.getByText('Email');
    expect(label).toHaveAttribute('for', 'email');
    expect(label).toHaveClass('fixture-label');
    expect(label).toHaveAttribute('data-auth', 'label');
    expect(screen.getByText('We never share it.')).toHaveClass('fixture-hint');
    // The error is announced, not merely coloured: a frame that dropped
    // `role="alert"` would leave a screen reader with no notice of it.
    expect(screen.getByRole('alert')).toHaveClass('fixture-error');
  });

  it('keeps the neutral field action after the control in keyboard order', () => {
    withAppearance(
      <AuthField id="password" label="Password" action={<a href="/forgot-password">Forgot?</a>}>
        <input id="password" />
      </AuthField>,
    );
    const input = screen.getByLabelText('Password');
    const link = screen.getByRole('link', { name: 'Forgot?' });
    expect(input.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(link.closest('[data-auth="field-action"]')).not.toBeNull();
    const focusable = input.closest('[data-auth="field"]')!.querySelectorAll('input, a');
    expect([...focusable]).toEqual([input, link]);
  });

  it('keeps the notice roles, which callers rely on for tone', () => {
    withAppearance(
      <>
        <AuthNotice tone="error">Something went wrong</AuthNotice>
        <AuthNotice tone="ok">Check your inbox</AuthNotice>
        <AuthNotice>Heads up</AuthNotice>
      </>,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong');
    expect(screen.getAllByRole('status')).toHaveLength(2);
  });

  it('renders the loading state as a live region the module styles', () => {
    withAppearance(<AuthLoading />);

    const status = screen.getByRole('status');
    expect(status).toHaveClass('fixture-loading-wrap');
    expect(status.firstElementChild).toHaveClass('fixture-loading');
  });

  it('falls back to the neutral appearance outside any module boundary', () => {
    // Error pages and unit tests render the shared form with no boundary above
    // it. A form that cannot say its own label is worse than one wearing the
    // default look.
    render(
      <AuthField id="email" label="Email" error="Required">
        <input id="email" />
      </AuthField>,
    );

    // The label's own look comes from the layout, so what the default has to
    // provide is the *association*: a label bound to its control. The error
    // keeps its class, because that one is a plain appearance string.
    const label = screen.getByText('Email');
    expect(label).toHaveAttribute('for', 'email');
    expect(label).toHaveAttribute('data-auth', 'label');
    expect(screen.getByRole('alert')).toHaveClass('text-red-600');
  });
});
