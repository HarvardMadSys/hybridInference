// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';

const navigation = vi.hoisted(() => ({ pathname: '/dashboard' }));
const auth = vi.hoisted(() => ({
  state: {
    loading: false,
    isAuthenticated: true,
    user: null as null | {
      id: string;
      email: string | null;
      role: string;
      is_admin: boolean;
    },
  },
}));

vi.mock('next/navigation', () => ({
  usePathname: () => navigation.pathname,
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: auth.state }),
}));

import { ConfigurationBanner } from './ConfigurationBanner';

const ADMIN = { id: 'u1', email: null, role: 'admin', is_admin: true };
const MEMBER = { id: 'u2', email: 'member@example.test', role: 'free', is_admin: false };

function renderBanner({
  incomplete = true,
  required = false,
  pathname = '/dashboard',
}: { incomplete?: boolean; required?: boolean; pathname?: string } = {}) {
  navigation.pathname = pathname;
  return render(
    <SiteConfigProvider
      initialConfig={{
        ...buildTimeSiteConfig,
        setup: { required },
        configuration: { incomplete },
      }}
    >
      <ConfigurationBanner />
    </SiteConfigProvider>,
  );
}

beforeEach(() => {
  auth.state = { loading: false, isAuthenticated: true, user: ADMIN };
});

afterEach(() => {
  cleanup();
});

describe('ConfigurationBanner', () => {
  it('tells an administrator what to fix, and where', () => {
    renderBanner();

    const banner = screen.getByRole('status');
    expect(banner).toHaveTextContent('Required settings are missing.');
    expect(screen.getByRole('link', { name: 'Open Configuration' })).toHaveAttribute(
      'href',
      '/dashboard/admin/configuration?missing=1',
    );
  });

  it('leaves the Configuration tab to list the missing settings itself', () => {
    renderBanner({ pathname: '/dashboard/admin/configuration' });

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('asks everyone else to contact the administrator', () => {
    auth.state.user = MEMBER;
    renderBanner();

    expect(screen.getByRole('status')).toHaveTextContent(
      'This service is missing required configuration, so some features may not work. Please contact your administrator.',
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('shows nothing to an anonymous visitor', () => {
    auth.state = { loading: false, isAuthenticated: false, user: null };
    renderBanner();

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('shows nothing when the configuration is complete', () => {
    renderBanner({ incomplete: false });

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it.each(['/setup', '/', '/login', '/signup'])('stays off %s', (pathname) => {
    renderBanner({ pathname });

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('stays off while setup is pending', () => {
    renderBanner({ required: true });

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
