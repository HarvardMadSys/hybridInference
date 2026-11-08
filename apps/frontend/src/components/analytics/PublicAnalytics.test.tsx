// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ pathname: '/' }));

vi.mock('next/navigation', () => ({ usePathname: () => nav.pathname }));

type Module = typeof import('./PublicAnalytics');

const COUNTER_SRC = 'https://www.statcounter.com/counter/counter.js';

let PublicAnalytics: Module['PublicAnalytics'];
let isAnalyticsPath: Module['isAnalyticsPath'];
let address: { pathname: string; reload: ReturnType<typeof vi.fn> };
let idle: (() => void)[];

function analytics(signedOut = true) {
  return (
    <PublicAnalytics projectId="12345" securityKey="key" nonce="n0nce" signedOut={signedOut} />
  );
}

/** The browser goes idle after the page has loaded: what the counter waits for. */
async function settle(): Promise<void> {
  for (const callback of idle.splice(0)) callback();
  await new Promise((resolve) => setTimeout(resolve, 10));
}

/** Move to `path` as the console does: the address changes, then the page draws. */
function navigate(path: string, rerender: (ui: React.ReactElement) => void) {
  address.pathname = path;
  nav.pathname = path;
  rerender(analytics());
}

function counter(): HTMLScriptElement | null {
  return document.querySelector(`script[src="${COUNTER_SRC}"]`);
}

beforeEach(async () => {
  // The counter loads once per document, which is module state.
  vi.resetModules();
  ({ PublicAnalytics, isAnalyticsPath } = await import('./PublicAnalytics'));
  nav.pathname = '/';
  idle = [];
  address = { ...window.location, pathname: '/', reload: vi.fn() };
  vi.stubGlobal('location', address);
  vi.stubGlobal('requestIdleCallback', (callback: () => void) => idle.push(callback));
  vi.stubGlobal('cancelIdleCallback', (handle: number) => {
    idle[handle - 1] = () => undefined;
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  for (const script of document.querySelectorAll('script')) script.remove();
});

describe('PublicAnalytics', () => {
  it('loads the counter on a public page for a visitor with no session', async () => {
    render(analytics());
    await settle();

    expect(counter()).not.toBeNull();
    // The page's policy runs no script without the response's nonce.
    expect(counter()?.nonce).toBe('n0nce');
    expect(window).toMatchObject({ sc_project: 12345, sc_invisible: 1, sc_security: 'key' });
  });

  it('waits for the page to load and the browser to go idle', () => {
    render(analytics());

    expect(counter()).toBeNull();
  });

  // A third-party script can read the access token and call /auth/refresh.
  it('never loads for a visitor who has a session', async () => {
    const { container } = render(analytics(false));
    await settle();

    expect(counter()).toBeNull();
    expect(container.innerHTML).not.toContain('statcounter');
  });

  it('never loads on the console, the account forms or a page with a token in its URL', async () => {
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
      address.pathname = path;
      const { container } = render(analytics());
      await settle();
      expect(counter(), path).toBeNull();
      expect(container.innerHTML, path).not.toContain('statcounter');
      cleanup();
    }
  });

  it('does not start on a public page reached from one where it could not load', async () => {
    nav.pathname = '/login';
    address.pathname = '/login';
    const { rerender } = render(analytics());

    navigate('/', rerender);
    await settle();

    expect(counter()).toBeNull();
  });

  // The visitor clicked "Sign in" before the page finished loading: the
  // counter must not arrive on the sign-in form.
  it('does not load once the visitor has left the public pages', async () => {
    const { rerender } = render(analytics());

    navigate('/login', rerender);
    await settle();

    expect(counter()).toBeNull();
  });

  it('loads on another public page the visitor moved to meanwhile', async () => {
    const { rerender } = render(analytics());

    navigate('/team', rerender);
    await settle();

    expect(counter()).not.toBeNull();
  });

  it('adds the counter once, however often the page draws', async () => {
    const { rerender } = render(analytics());
    navigate('/stats', rerender);
    navigate('/', rerender);
    await settle();

    expect(document.querySelectorAll(`script[src="${COUNTER_SRC}"]`)).toHaveLength(1);
  });

  // The console moves between pages without reloading, and a loaded script stays.
  it('reloads the page when the visitor leaves the public pages after it has loaded', async () => {
    const { rerender } = render(analytics());
    await settle();
    counter()?.dispatchEvent(new Event('load'));

    navigate('/team', rerender);
    expect(address.reload).not.toHaveBeenCalled();

    navigate('/login', rerender);
    expect(address.reload).toHaveBeenCalledOnce();
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
