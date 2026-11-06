// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ pathname: '/' }));
const scripts = vi.hoisted(() => ({ onLoad: [] as (() => void)[] }));

vi.mock('next/navigation', () => ({ usePathname: () => nav.pathname }));
vi.mock('next/script', () => ({
  default: ({
    id,
    src,
    nonce,
    children,
    onLoad,
  }: {
    id?: string;
    src?: string;
    nonce?: string;
    children?: React.ReactNode;
    onLoad?: () => void;
  }) => {
    if (onLoad) scripts.onLoad.push(onLoad);
    // An inert stand-in for the element `next/script` would add.
    return (
      <div data-script={id} data-src={src} data-nonce={nonce}>
        {children}
      </div>
    );
  },
}));

import { isAnalyticsPath, PublicAnalytics } from './PublicAnalytics';

const reload = vi.fn();

function analytics(signedOut = true) {
  return (
    <PublicAnalytics projectId="12345" securityKey="key" nonce="n0nce" signedOut={signedOut} />
  );
}

beforeEach(() => {
  nav.pathname = '/';
  scripts.onLoad = [];
  reload.mockReset();
  vi.stubGlobal('location', { ...window.location, reload });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('PublicAnalytics', () => {
  it('loads the counter on a public page for a visitor with no session', () => {
    const { container } = render(analytics());

    expect(container.querySelector('[data-script="statcounter-config"]')?.textContent).toContain(
      'var sc_project=12345',
    );
    expect(container.querySelector('[data-script="statcounter-loader"]')).toHaveAttribute(
      'data-src',
      'https://www.statcounter.com/counter/counter.js',
    );
    // The page's policy runs no script without the response's nonce.
    expect(container.querySelector('[data-script="statcounter-loader"]')).toHaveAttribute(
      'data-nonce',
      'n0nce',
    );
  });

  // A third-party script can read the access token and call /auth/refresh.
  it('never loads for a visitor who has a session', () => {
    const { container } = render(analytics(false));

    expect(container.querySelector('[data-script]')).toBeNull();
  });

  it('never loads on the console, the account forms or a page with a token in its URL', () => {
    for (const path of [
      '/dashboard',
      '/dashboard/admin/users',
      '/login',
      '/signup',
      '/forgot-password',
      '/reset-password',
      '/verify-email',
      '/authorize',
      '/chat',
    ]) {
      nav.pathname = path;
      const { container } = render(analytics());
      expect(container.querySelector('[data-script]'), path).toBeNull();
      expect(container.innerHTML, path).not.toContain('statcounter');
      cleanup();
    }
  });

  it('does not start on a public page reached from one where it could not load', () => {
    nav.pathname = '/login';
    const { container, rerender } = render(analytics());

    nav.pathname = '/';
    rerender(analytics());

    expect(container.querySelector('[data-script]')).toBeNull();
  });

  // The console moves between pages without reloading, and a loaded script stays.
  it('reloads the page when the visitor leaves the public pages after it has loaded', () => {
    const { rerender } = render(analytics());
    for (const onLoad of scripts.onLoad) onLoad();

    nav.pathname = '/team';
    rerender(analytics());
    expect(reload).not.toHaveBeenCalled();

    nav.pathname = '/login';
    rerender(analytics());
    expect(reload).toHaveBeenCalledOnce();
  });

  it('does not reload when the counter never loaded', () => {
    const { rerender } = render(analytics());

    nav.pathname = '/login';
    rerender(analytics());

    expect(reload).not.toHaveBeenCalled();
  });
});

describe('isAnalyticsPath', () => {
  it('matches the public pages, with or without a trailing slash', () => {
    expect(isAnalyticsPath('/')).toBe(true);
    expect(isAnalyticsPath('/stats/')).toBe(true);
    expect(isAnalyticsPath('/terms')).toBe(true);
    expect(isAnalyticsPath('/team')).toBe(true);
    expect(isAnalyticsPath('/teams')).toBe(false);
    expect(isAnalyticsPath(null)).toBe(false);
  });
});
