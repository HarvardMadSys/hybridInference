// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { Catalog } from '@/lib/i18n/translate';
import zhCN from '@/lib/i18n/messages/zh-CN.json';
import { Header } from './Header';

const replace = vi.fn();
vi.mock('@/components/providers/ThemeProvider', () => ({
  useTheme: () => ({ theme: 'system', resolvedTheme: 'light', setTheme: vi.fn() }),
}));
let authState: {
  isAuthenticated: boolean;
  user?: { id: string; user_name: string; role: string };
} = {
  isAuthenticated: false,
};

const navigation = vi.hoisted(() => ({ pathname: '/dashboard' }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  usePathname: () => navigation.pathname,
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: authState,
    accounts: [],
    maxAccounts: 5,
    logout: vi.fn(),
    logoutAll: vi.fn(),
    switchAccount: vi.fn(),
  }),
}));

const configuredBranding = {
  appName: 'Example Inference',
  orgName: 'Example Org',
  orgUrl: 'https://org.example.test',
  statusUrl: 'https://status.example.test/',
  logoUrl: '/site-assets/logo.svg',
  navLinks: [{ label: 'Example Project', url: 'https://project.example.test/' }],
};

let brandingOverride: Record<string, unknown> = configuredBranding;
let featuresOverride: Record<string, unknown> = { rag: true, publicStats: false };

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({ branding: brandingOverride, features: featuresOverride }),
}));

describe('Header', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    replace.mockClear();
    navigation.pathname = '/dashboard';
    brandingOverride = configuredBranding;
    featuresOverride = { rag: true, publicStats: false };
    authState = {
      isAuthenticated: false,
    };
  });

  it('hides the dashboard link from guests', () => {
    render(<Header />);

    expect(screen.queryByRole('link', { name: /dashboard/i })).not.toBeInTheDocument();
  });

  it('leaves the status link to the footer', () => {
    render(<Header />);
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }));

    // Neither the bar nor the phone menu links it, though a status page is configured.
    expect(screen.queryByRole('link', { name: 'Status' })).not.toBeInTheDocument();
  });

  it('renders a same-origin runtime logo without a build-time image allowlist', () => {
    render(<Header />);

    expect(screen.getByRole('img', { name: 'Example Inference logo' })).toHaveAttribute(
      'src',
      '/site-assets/logo.svg',
    );
  });

  it('shows a distribution nav link to guests', () => {
    render(<Header />);

    const navLink = screen.getByRole('link', { name: 'Example Project' });

    expect(navLink).toHaveAttribute('href', 'https://project.example.test/');
    expect(navLink).toHaveAttribute('target', '_blank');
    expect(navLink).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('places distribution nav links in the order the distribution listed them', () => {
    brandingOverride = {
      ...configuredBranding,
      navLinks: [
        { label: 'Example Project', url: 'https://project.example.test/' },
        { label: 'Example Forum', url: 'https://forum.example.test/' },
      ],
    };

    render(<Header />);

    const labels = screen
      .getAllByRole('link')
      .map((node) => node.textContent?.trim())
      .filter((label) => ['Example Project', 'Example Forum'].includes(label ?? ''));

    expect(labels).toEqual(['Example Project', 'Example Forum']);
  });

  it('shows a docs link only when the deployment configures a docs URL', () => {
    render(<Header />);

    expect(screen.queryByRole('link', { name: 'Docs' })).not.toBeInTheDocument();

    cleanup();
    brandingOverride = { ...configuredBranding, docsUrl: 'https://docs.example.test/' };

    render(<Header />);

    const docsLink = screen.getByRole('link', { name: 'Docs' });

    expect(docsLink).toHaveAttribute('href', 'https://docs.example.test/');
    expect(docsLink).toHaveAttribute('target', '_blank');
    expect(docsLink).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('hides the stats link unless the deployment publishes stats', () => {
    render(<Header />);

    expect(screen.queryByRole('link', { name: 'Stats' })).not.toBeInTheDocument();
  });

  it('shows guests a same-tab stats link when the deployment publishes stats', () => {
    featuresOverride = { rag: true, publicStats: true };

    render(<Header />);

    const statsLink = screen.getByRole('link', { name: 'Stats' });

    expect(statsLink).toHaveAttribute('href', '/stats');
    expect(statsLink).not.toHaveAttribute('target');
  });

  it('places the stats link before the distribution nav links', () => {
    featuresOverride = { rag: true, publicStats: true };

    render(<Header />);

    const labels = screen
      .getAllByRole('link')
      .map((node) => node.textContent?.trim())
      .filter((label) => ['Stats', 'Example Project'].includes(label ?? ''));

    expect(labels).toEqual(['Stats', 'Example Project']);
  });

  it('renders no extra header links when the distribution lists none', () => {
    brandingOverride = { ...configuredBranding, navLinks: [] };

    render(<Header />);

    expect(screen.queryByRole('link', { name: 'Example Project' })).not.toBeInTheDocument();
  });

  it('offers guests no language button in the header', () => {
    render(<Header />);

    // The phone's Menu button is the only one: it holds links, not languages.
    expect(
      screen.getAllByRole('button').map((button) => button.getAttribute('aria-label')),
    ).toEqual(['Menu']);
  });

  it('offers the links under a Menu button for a phone', () => {
    authState = { isAuthenticated: true, user: { id: 'ada', user_name: 'Ada', role: 'free' } };

    render(<Header />);

    const button = screen.getByRole('button', { name: 'Menu' });
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('navigation', { name: 'Menu' })).not.toBeInTheDocument();

    fireEvent.click(button);

    expect(button).toHaveAttribute('aria-expanded', 'true');
    const menu = screen.getByRole('navigation', { name: 'Menu' });
    expect(button).toHaveAttribute('aria-controls', menu.id);
    expect(
      within(menu)
        .getAllByRole('link')
        .map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      ['Example Project', 'https://project.example.test/'],
      ['Docs Assistant', '/chat'],
      ['Dashboard', '/dashboard'],
      // On a phone the organization is listed here rather than in the bar.
      ['Example Org', 'https://org.example.test'],
    ]);
    expect(within(menu).getByRole('link', { name: 'Example Project' })).toHaveAttribute(
      'target',
      '_blank',
    );
  });

  it('closes the menu on Escape, returning focus to its button', () => {
    render(<Header />);
    const button = screen.getByRole('button', { name: 'Menu' });
    fireEvent.click(button);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('navigation', { name: 'Menu' })).not.toBeInTheDocument();
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(button).toHaveFocus();
  });

  it('closes the menu once a link in it is followed', () => {
    featuresOverride = { rag: true, publicStats: true };
    render(<Header />);
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }));

    fireEvent.click(
      within(screen.getByRole('navigation', { name: 'Menu' })).getByRole('link', {
        name: 'Stats',
      }),
    );

    expect(screen.queryByRole('navigation', { name: 'Menu' })).not.toBeInTheDocument();
  });

  it('offers no Menu button when there is nothing to list', () => {
    brandingOverride = { appName: 'Bare Gateway', orgName: '', orgUrl: '', navLinks: [] };
    featuresOverride = { rag: false, publicStats: false };

    render(<Header />);

    expect(screen.queryByRole('button', { name: 'Menu' })).not.toBeInTheDocument();
  });

  it('shows a dashboard link in the header for authenticated users', () => {
    authState = {
      isAuthenticated: true,
    };

    render(<Header />);

    expect(screen.getByRole('link', { name: /dashboard/i })).toHaveAttribute('href', '/dashboard');
  });
});

describe('Header, across the dashboard', () => {
  afterEach(() => {
    cleanup();
    navigation.pathname = '/dashboard';
  });

  it('is a bar across the window that stays in view over the user console', () => {
    navigation.pathname = '/dashboard/keys';
    render(<Header />);

    const header = screen.getByRole('banner');
    expect(header).toHaveClass('border-b', 'md:sticky');
    expect(header.className).not.toMatch(/max-w-/);
  });

  it('is the same bar over the admin area, where the tables keep their own sticky rows', () => {
    navigation.pathname = '/dashboard/admin/users';
    render(<Header />);

    const header = screen.getByRole('banner');
    expect(header).toHaveClass('border-b');
    expect(header).not.toHaveClass('md:sticky');
  });

  it('is as wide as the page on every other route', () => {
    navigation.pathname = '/login';
    render(<Header />);

    const header = screen.getByRole('banner');
    expect(header).toHaveClass('max-w-5xl');
    expect(header).not.toHaveClass('border-b');
  });
});

describe('Header, for a reader who chose Chinese', () => {
  afterEach(cleanup);

  it('shows the console links in Chinese and offers English by name', () => {
    brandingOverride = configuredBranding;
    featuresOverride = { rag: true, publicStats: true };
    authState = { isAuthenticated: true, user: { id: 'ada', user_name: 'Ada', role: 'admin' } };

    render(
      <LocaleProvider locale="zh-CN" catalog={zhCN as Catalog} moduleLocale="">
        <Header />
      </LocaleProvider>,
    );

    for (const name of ['统计', '文档助手', '控制台']) {
      expect(screen.getByRole('link', { name })).toBeInTheDocument();
    }
    expect(screen.getByRole('img', { name: 'Example Inference 标志' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'English' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Ada' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '语言' }));
    expect(screen.getByRole('menuitemradio', { name: 'English' })).toHaveAttribute('lang', 'en');
    expect(screen.getByRole('menuitemradio', { name: '简体中文' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });
});
